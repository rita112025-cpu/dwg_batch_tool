"""accore_launch: incremental console decoding and marker scanning."""
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import accore_launch  # noqa: E402


def test_console_text_matches_full_decode_across_any_split():
    # CJK text plus a non-BMP character (surrogate pair) so splits land mid-code-unit and mid-pair.
    raw = "開啟 圖面A.dwg 😀 DWGBATCH_OPEN_0|圖面A.dwg|\r\nDWGBATCH_DONE_0_".encode("utf-16-le")
    for split in range(1, len(raw)):
        console = accore_launch.ConsoleText()
        buf = bytearray()
        for part in (raw[:split], raw[split:]):
            buf.extend(part)
            console.feed(buf)
        assert console.text == raw.decode("utf-16-le"), split
        assert console.byte_count == len(raw)


def test_console_text_feed_reports_new_bytes_only():
    console = accore_launch.ConsoleText()
    buf = bytearray("ab".encode("utf-16-le"))
    assert console.feed(buf) is True
    assert console.feed(buf) is False
    buf.extend("c".encode("utf-16-le"))
    assert console.feed(buf) is True
    assert console.text == "abc"


def test_marker_scan_start_keeps_the_unfinished_line():
    assert accore_launch.marker_scan_start("no newline yet", 0) == 0
    text = "line one\r\nDWGBATCH_OP"
    assert accore_launch.marker_scan_start(text, 0) == text.index("DWGBATCH")
    assert accore_launch.marker_scan_start(text + "EN_0|a|\r\nnext", 3) == len(text + "EN_0|a|\r\n")


FAKE = textwrap.dedent('''
    import sys, time
    out = sys.stdout.buffer
    def say(s, pause=0.35):
        out.write(s.encode("utf-16-le")); out.flush(); time.sleep(pause)
    say("Command: (princ (strcat \\"DWGB\\" \\"ATCH_OPEN_0|\\" ...))\\r\\n")
    for i, name in enumerate(["圖面A.dwg", "B 圖.dwg"]):
        say("noise " * 50 + "DWGBATCH_OP")        # marker cut across two reads
        say("EN_%d|%s|\\r\\n" % (i, name))
        say("saving " * 50 + "DWGBATCH_DO")
        say("NE_%d_\\r\\n" % i)
''')


def test_run_launch_finds_markers_split_across_reads(tmp_path):
    fake = tmp_path / "fake_accore.py"
    fake.write_text(FAKE, encoding="utf-8")
    lisp = tmp_path / "work" / "job.lsp"
    lisp.parent.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    jobs = [{"name": "a"}, {"name": "b"}]
    started = []

    consumed, stopped, fail_msg, stdout = accore_launch.run_launch(
        jobs, fake, lisp, "(princ)\n", 30, None, lambda job: started.append(job["name"]), logs, "unit",
    )

    assert (consumed, stopped, fail_msg) == (2, False, "")
    assert started == ["a", "b"]
    assert [job["opened_name"] for job in jobs] == ["圖面A.dwg", "B 圖.dwg"]
    assert jobs[0]["console"].endswith("DWGBATCH_DONE_0_")
    assert jobs[1]["console"].startswith("\r\nnoise") and jobs[1]["console"].endswith("DWGBATCH_DONE_1_")
    assert "DWGBATCH_DONE_1_" in stdout
    assert len(list(logs.glob("unit_batch_*.stdout.log"))) == 1
    assert not list(lisp.parent.glob("unit_*.scr"))
