"""GUI-facing orchestration. No tkinter here; safe to run in a worker thread."""
import datetime
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import resolve, log_error
import env_check
import batch_convert
import analyze_dxf
import find_scada

DEFAULT_OPTS = dict(recursive=True, convert=True, analyze=True, scada=True,
                    csv=True, json=True, overwrite=False)


def run_multiformat(cfg, inputs=None, log=None, progress=None, stop_event=None, compare=None):
    """Independent mixed-format entrypoint; legacy CAD entrypoint remains compatible."""
    import csv
    import importlib
    import json
    import os
    import shutil
    import tempfile
    from engineering_data import Store, digest, evidence, UnsupportedFormat
    import cross_reference
    start = time.time()
    out = resolve(cfg, 'output_dir').resolve()
    sources = [Path(p).resolve() for p in (inputs or [resolve(cfg, 'input_dir')])]
    if compare: sources.append(Path(compare).resolve())
    if any(p == out or p.is_relative_to(out) for p in sources):
        raise ValueError('Output must not contain the input folder or equal a selected input')
    routes = {'.ifc': 'ifc', '.xlsx': 'excel', '.xlsm': 'excel', '.csv': 'excel', '.pdf': 'pdf', '.docx': 'docx', '.xml': 'navisworks', '.html': 'navisworks', '.htm': 'navisworks', '.dwg': 'cad', '.dxf': 'cad', '.nwd': 'unsupported'}
    files = set()
    for source in sources:
        if not source.exists(): raise FileNotFoundError(source)
        iterator = (source.rglob('*') if cfg.get('recursive', True) else source.glob('*')) if source.is_dir() else [source]
        for p in iterator:
            if p.is_file() and not p.is_relative_to(out) and (p.suffix.lower() in routes or not source.is_dir()): files.add(p)
    if compare: files.add(Path(compare).resolve())
    files = sorted(files)
    import revision_meta
    revisions = revision_meta.infer(files)   # older/newer order only; formal `revision` stays NULL
    out.mkdir(parents=True, exist_ok=True)
    dbdir = out / 'database'
    dbdir.mkdir(exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='project_', suffix='.db', dir=dbdir)
    os.close(fd)
    store = Store(temporary)
    summary = dict(total=len(files), ok=0, failed=0, skipped=0, warnings=0, failed_files=[], stopped=False, fatal='', dwg=0, dxf=0, keyword_hits=0, unique_objects=0, suspect_texts=0)
    log = log or (lambda level, msg: None)
    try:
        for i, path in enumerate(files, 1):
            if stop_event is not None and stop_event.is_set():
                summary['stopped'] = True
                break
            kind = routes.get(path.suffix.lower(), 'unsupported')
            if progress: progress(kind.upper(), i, len(files), str(path))
            status, error, hash_before = 'OK', '', ''
            store.db.execute('SAVEPOINT file_parse')
            try:
                hash_before = digest(path)
                if path.suffix.lower() == '.csv':
                    with path.open(encoding='utf-8-sig', newline='') as f:
                        header = next(csv.reader(f), [])
                    if {'clash_id', 'clash_name', 'object_a', 'object_b', 'status'} <= set(header): kind = 'navisworks'
                if kind == 'cad':
                    status = _parse_cad(path, store, cfg, out, stop_event)
                    summary[path.suffix.lower()[1:]] += 1
                elif kind == 'unsupported':
                    raise UnsupportedFormat(f'Unsupported format: {path.suffix}')
                else:
                    status = importlib.import_module('analyze_' + kind).parse(path, store, cfg)
                if digest(path) != hash_before: raise RuntimeError('Source hash changed during analysis')
                if status == 'OK': summary['ok'] += 1
                else: summary['warnings'] += 1
            except Exception as exc:
                store.db.execute('ROLLBACK TO file_parse')
                status = 'SKIPPED_DEPENDENCY' if isinstance(exc, ImportError) else ('UNSUPPORTED_FORMAT' if isinstance(exc, UnsupportedFormat) else 'FAILED')
                error = f'{type(exc).__name__}: {exc}'
                if isinstance(exc, ImportError): error += '; install the parser dependency listed in requirements.txt / requirements-ifc.txt'
                summary['failed'] += 1
                summary['skipped'] += status == 'SKIPPED_DEPENDENCY'
                summary['failed_files'].append(f'{path}: {status}: {error}')
                log_error(cfg, f'file={path} stage={kind} {error}')
            finally:
                store.db.execute('RELEASE file_parse')
            store.add('project_files', evidence(path, path.suffix.lstrip('.').upper(), file_id=__import__('hashlib').sha256(str(path).encode()).hexdigest(), file_hash=hash_before, parsed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), status=status, error=error, **revisions.get(path, {})))
            store.db.commit()
            log('OK' if status == 'OK' else 'WARN', f'{path.name}: {status} {error}')
        summary['keyword_hits'] = store.db.execute("SELECT count(*) FROM exports WHERE report IN ('cad/scada_hits.csv', 'pdf/pdf_hits.csv')").fetchone()[0]
        summary['unique_objects'] = store.db.execute("SELECT count(*) FROM exports WHERE report='cad/object_hits.csv'").fetchone()[0]
        cross_reference.run(store, cfg, str(Path(compare).resolve()) if compare else None)
        if compare:
            from analyze_excel import compare as compare_boq
            old_path = str(Path(compare).resolve())
            old, new = [], []
            for row in store.rows('boq_items'):
                (old if row['source_file'] == old_path else new).append(row)
            if len({r['source_file'] for r in new}) > 1:
                raise ValueError('BOQ revision comparison requires exactly one new BOQ source; select that file explicitly')
            for row in compare_boq(old, new): store.report('excel/boq_compare.csv', row)
        store.db.commit()
        staging = Path(tempfile.mkdtemp(prefix='.publish_', dir=out))
        keep_staging = False
        try:
            store.export(staging / 'new')
            _empty_reports(staging / 'new', store)
            store.db.close()
            _publish(staging, out, Path(temporary), dbdir / 'project.db')
        except _RollbackIncomplete:
            keep_staging = True   # originals still sit in staging/old; do not delete them
            raise
        finally:
            if not keep_staging: shutil.rmtree(staging, ignore_errors=True)
    except BaseException:
        store.db.close()
        Path(temporary).unlink(missing_ok=True)
        raise
    summary.update(elapsed=time.time()-start, exit_code=int(bool(summary['failed'] or summary['warnings'] or summary['stopped'])))
    log('WARN' if summary['exit_code'] else 'OK', 'Completed with warnings' if summary['exit_code'] else 'Completed')
    return summary


class _RollbackIncomplete(RuntimeError):
    pass


def _publish(staging, out, temp_db, final_db):
    """Move staged reports, then project.db last, into out. Existing files are moved aside first;
    a failing move restores them and removes new files, so out is left as it was. A process kill
    skips the restore (staging/old keeps the originals)."""
    import os
    moves = [(src, out / src.relative_to(staging / 'new')) for src in sorted(p for p in (staging / 'new').rglob('*') if p.is_file())]
    moves.append((temp_db, final_db))
    done, made_dirs = [], []
    try:
        for src, dest in moves:
            for d in reversed([dest.parent, *dest.parent.parents]):
                if not d.exists(): d.mkdir(); made_dirs.append(d)
            backup = None
            if dest.exists():
                backup = staging / 'old' / dest.relative_to(out)
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(dest, backup)
            done.append((dest, backup))   # recorded before the move so a failed move still restores the original
            os.replace(src, dest)
    except BaseException as exc:
        errors = []
        for dest, backup in reversed(done):
            try:
                if backup: os.replace(backup, dest)
                else: dest.unlink(missing_ok=True)
            except OSError as e:
                errors.append(f'{dest}: {e}')
        for d in reversed(made_dirs):
            try: d.rmdir()
            except OSError: pass
        if errors:
            raise _RollbackIncomplete(f'Publish failed ({type(exc).__name__}: {exc}) and {len(errors)} file(s) could not be restored; originals kept in {staging / "old"}: ' + '; '.join(errors)) from exc
        raise


def _empty_reports(out, store):
    from engineering_data import Store, COMMON
    reports = {'cad': ['layers','texts','blocks','dimensions','attribs','file_index','scada_hits','scada_excluded','object_hits','block_summary'], 'ifc': ['ifc_objects','ifc_systems','ifc_spaces','ifc_summary'], 'excel': ['excel_sheets','excel_tables','boq_items','boq_summary','boq_compare'], 'pdf': ['pdf_pages','pdf_sections','pdf_hits','pdf_summary','boq_items','boq_skipped'], 'docx': ['docx_sections','docx_tables','requirements'], 'navisworks': ['navis_clashes','navis_summary'], 'cross_reference': ['cross_reference', 'coverage']}
    for folder, names in reports.items():
        for name in names:
            path = out / folder / (name + '.csv')
            if not store.db.execute('SELECT 1 FROM exports WHERE report=? LIMIT 1', (folder+'/'+name+'.csv',)).fetchone():
                from engineering_data import SCHEMA
                table = {'ifc_objects': 'engineering_objects', 'ifc_systems': 'engineering_objects', 'ifc_spaces': 'engineering_objects', 'boq_items': 'boq_items', 'requirements': 'requirements', 'navis_clashes': 'clashes', 'cross_reference': 'cross_reference_results', 'pdf_pages': 'documents', 'boq_skipped': 'document_sections', 'pdf_hits': 'documents', 'pdf_sections': 'document_sections', 'docx_sections': 'document_sections', 'docx_tables': 'document_sections', 'excel_tables': 'document_sections'}.get(name)
                Store._csv(path, [], SCHEMA.get(table, COMMON))


def _parse_cad(path, store, cfg, out, stop_event):
    import json
    from engineering_data import evidence, digest
    from convert_single import convert
    token = __import__('hashlib').sha256(str(path).encode()).hexdigest()[:12]
    target = path
    if path.suffix.lower() == '.dwg':
        target = out / 'cad' / 'dxf' / (path.stem + '_' + token + '.dxf')
        result = convert(path, target, cfg['accoreconsole'], cfg, overwrite=True, stop_event=stop_event)
        if result['status'] != 'DXF_VALID': raise RuntimeError(str(result))
    import ezdxf
    doc = ezdxf.readfile(target)
    rows, info = analyze_dxf.analyze_doc(doc, str(path))
    attributes = {}
    for attr in rows['attribs']:
        attributes.setdefault(attr.get('parent_handle'), []).append(attr)
    spec_tags = {tag.upper() for tag in cfg.get('specification_attribute_tags', ['SPECIFICATION', 'SPEC'])}
    for category, values in rows.items():
        if category == 'entity_types': continue
        for row in values:
            store.report(f'cad/{category}.csv', dict(row, **evidence(path, 'CAD', row.get('handle', category))))
            if category in ('blocks', 'texts'):
                values = {k: row[k] for k in ('layer','handle','x','y','z','context') if k in row}
                props = dict(row)
                if category == 'blocks':
                    props['attributes'] = attributes.get(row.get('handle'), [])
                    specs = {a['text'] for a in props['attributes'] if a.get('tag','').upper() in spec_tags}
                    values['specification'] = next(iter(specs)) if len(specs)==1 else None
                store.add('engineering_objects', evidence(path, 'CAD', row.get('handle', ''), object_id=token+':'+row.get('handle', ''), object_type='INSERT' if category == 'blocks' else row['entity_type'], name=row.get('block_name', row.get('text', '')), properties_json=json.dumps(props, ensure_ascii=False), **values))
    json_dir = out / 'cad' / 'json'
    json_dir.mkdir(parents=True, exist_ok=True)
    (json_dir / (path.stem + '_' + token + '.json')).write_text(json.dumps(dict(index=info, **rows), ensure_ascii=False), encoding='utf-8')
    import scada_rules
    rules = scada_rules.build_rules(cfg)
    hits, excluded = find_scada.search_doc(doc, str(path), rules)
    for name, values in [('scada_hits', hits), ('scada_excluded', excluded), ('object_hits', scada_rules.build_object_hits(hits)), ('block_summary', scada_rules.build_block_summary(rows['blocks'], rules, rows['attribs']))]:
        for row in values: store.report('cad/'+name+'.csv', dict(row, **evidence(path, 'CAD', row.get('handle', ''))))
    store.report('cad/file_index.csv', evidence(path, 'CAD', **info, status='OK'))
    return 'OK'


def scan(input_dir, recursive=True):
    """Read-only scan. Never touches AutoCAD or modifies files."""
    root = Path(input_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"input folder not found: {root}")
    it = root.rglob("*") if recursive else root.glob("*")
    dwgs, dxfs, size, folders = [], 0, 0, set()
    for p in it:
        try:
            if p.is_dir():
                folders.add(p)
            elif p.suffix.lower() == ".dwg":
                dwgs.append(p)
                size += p.stat().st_size
            elif p.suffix.lower() == ".dxf":
                dxfs += 1
        except OSError:
            continue
    names = {}
    for p in dwgs:
        names.setdefault(p.name.casefold(), []).append(p)
    return dict(
        dwg=len(dwgs), dxf=dxfs, size=size, folders=len(folders),
        chinese=sum(1 for p in dwgs if not p.name.isascii()),
        spaces=sum(1 for p in dwgs if " " in str(p)),
        duplicates=sum(1 for v in names.values() if len(v) > 1),
    )


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def fmt_duration(sec):
    sec = int(sec)
    return f"{sec // 60} 分 {sec % 60:02d} 秒"


def run_pipeline(cfg, opts, log=None, progress=None, stop_event=None):
    """Env check -> convert -> (validate inside convert) -> analyze -> scada -> summary.
    log(level, msg) ; progress(stage, i, n, current). Returns summary dict."""
    if opts.get('multiformat'):
        return run_multiformat(cfg, opts.get('inputs'), log, progress, stop_event, opts.get('compare'))
    opts = {**DEFAULT_OPTS, **opts}
    cfg = dict(cfg, recursive=opts["recursive"], overwrite=opts["overwrite"])
    out = resolve(cfg, "output_dir")
    logs = out / "logs"
    summary = dict(dwg=0, ok=0, failed=0, skipped=0, failed_files=[], analyze_failed=0, dxf=0, keyword_hits=0, unique_objects=0, suspect_texts=0,
                   elapsed=0.0,
                   stopped=False, fatal="")
    t0 = time.time()

    def _log(level, msg):
        line = f"[{datetime.datetime.now():%H:%M:%S}] {level:<5} {msg}"
        try:
            logs.mkdir(parents=True, exist_ok=True)
            with open(logs / "gui.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if log:
            log(level, msg)

    def _prog(stage):
        return lambda i, n, cur: progress(stage, i, n, cur) if progress else None

    def stopped():
        return stop_event is not None and stop_event.is_set()

    def finish():
        summary["elapsed"] = time.time() - t0
        return summary

    _log("INFO", "environment check")
    env = env_check.check(cfg)
    if not env["ok"]:
        summary["fatal"] = "environment check failed: " + "; ".join(env["lines"][-3:])
        _log("ERROR", summary["fatal"])
        log_error(cfg, summary["fatal"])
        return finish()
    try:
        logs.mkdir(parents=True, exist_ok=True)
        (out / "dxf").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        summary["fatal"] = f"output folder not writable: {e}"
        _log("ERROR", summary["fatal"])
        return finish()
    _log("OK", f"environment OK ({env['autocad']}, ezdxf {env['ezdxf']})")

    if opts["convert"]:
        dwg_files = batch_convert.list_dwg(cfg)
        n = len(dwg_files)
        _log("INFO", f"found {n} DWG")
        s = batch_convert.run(cfg, _log, _prog("DWG→DXF"), stop_event, files=dwg_files)
        summary.update(dwg=s["total"], ok=s["ok"], failed=s["failed"], skipped=s["skipped"])
        summary["failed_files"] += s["failed_files"]
        if s["fatal"]:
            summary["fatal"] = s["fatal"]
            _log("ERROR", s["fatal"])
            return finish()
        if s["stopped"]:
            summary["stopped"] = True
            _log("WARN", "stopped by user")
            return finish()
    dxf_files = sorted((out / "dxf").rglob("*.dxf"))
    summary["dxf"] = len(dxf_files)

    shared_search = None
    if opts["analyze"] and not stopped():
        _log("INFO", "analyzing DXF")
        if opts["scada"]:
            import scada_rules
            rules = scada_rules.build_rules(cfg)
            search_hits, search_excluded, search_failed = [], [], []

            def collect_search(doc, rel):
                try:
                    hits, excluded = find_scada.search_doc(doc, rel, rules)
                    search_hits.extend(hits)
                    search_excluded.extend(excluded)
                except Exception as exc:  # noqa: BLE001
                    message = f"{rel}: {type(exc).__name__}: {exc}"
                    search_failed.append(message)
                    log_error(cfg, f"find_scada {message}")
                    _log("ERROR", f"search {message}")
        else:
            collect_search = None
        stage = "Analyze + SCADA" if collect_search is not None else "Analyze"
        a = analyze_dxf.run(cfg, opts["csv"], opts["json"], _log, _prog(stage), stop_event,
                            files=dxf_files, document_callback=collect_search)
        if a["failed"]:
            _log("WARN", f"{a['failed']} DXF failed to analyze")
        summary["analyze_failed"] = a["failed"]
        summary["failed_files"] += [f"(analyze) {x}" for x in a["failed_files"]]
        summary["suspect_texts"] = a["suspect"]
        summary["stopped"] |= a["stopped"]
        if collect_search is not None:
            shared_search = (search_hits, search_excluded,
                             search_failed + list(a["failed_files"]), a["stopped"])
    if opts["scada"] and not stopped():
        _log("INFO", "SCADA keyword search")
        if shared_search is None:
            r = find_scada.run(cfg, _log, _prog("SCADA"), stop_event, files=dxf_files)
        else:
            r = find_scada.write_results(cfg, *shared_search, log=_log)
        summary["keyword_hits"] = r["hits"]
        summary["unique_objects"] = r["objects"]
        summary["failed_files"] += [f"(search) {x}" for x in r["failed_files"]]
        summary["stopped"] |= r["stopped"]
        _log("OK", f"Keyword Hits: {r['hits']}  Unique Matched Objects: {r['objects']}")
    if stopped():
        summary["stopped"] = True
        _log("WARN", "stopped by user")
    elif summary["failed_files"]:
        _log("WARN", f"Completed with warnings: {len(summary['failed_files'])} failure(s)")
        for x in summary["failed_files"]:
            _log("WARN", f"  failed: {x}")
    else:
        _log("OK", "done")
    return finish()


if __name__ == '__main__':
    from engineering_data import parser_cli
    sys.exit(parser_cli('mixed'))
