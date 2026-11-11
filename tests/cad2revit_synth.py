"""Synthetic DXF drawings for cad2revit tests. Nothing here is a project file."""

import ezdxf


def save(doc, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


def blank(insunits=4, insbase=(100, 200, 300)):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = insunits
    doc.header["$INSBASE"] = insbase
    doc.header["$MEASUREMENT"] = 1
    doc.header["$LUNITS"] = 2
    return doc


def sample_drawing(path):
    """One model with every entity Phase 1 reads, plus decoys that must not match."""
    doc = blank()
    msp = doc.modelspace()
    msp.add_line((0, 0, 1), (10, 0, 1), dxfattribs={"layer": "E-TRAY-MAIN"})
    msp.add_line((0, 1, 0), (4, 1, 2), dxfattribs={"layer": "E-TRAY-MAIN"})
    msp.add_lwpolyline(
        [(0, 0, 0, 0, 0.5), (5, 0, 0, 0, 0), (5, 5)],
        format="xyseb",
        close=True,
        dxfattribs={"layer": "E-COND-BRANCH", "elevation": 2.5},
    )
    poly = msp.add_polyline2d(
        [(0, 0), (1, 0), (1, 1)],
        close=True,
        dxfattribs={"layer": "E-TRAY-MAIN"},
    )
    poly.dxf.elevation = (0, 0, 4)
    msp.add_polyline3d([(0, 0, 1), (2, 0, 2)], dxfattribs={"layer": "E-TRAY-MAIN"})
    msp.add_arc(
        center=(0, 0, 3),
        radius=5,
        start_angle=0,
        end_angle=90,
        dxfattribs={"layer": "E-TRAY-MAIN"},
    )
    msp.add_text(
        "NOTE-A",
        dxfattribs={"layer": "E-NOTE", "height": 2.5, "rotation": 15, "insert": (1, 2, 3)},
    )
    msp.add_mtext(
        "合成\\P文字",
        dxfattribs={"layer": "E-NOTE", "char_height": 3.5, "rotation": 30, "insert": (4, 5, 6)},
    )
    msp.add_line((0, 0, 0), (1, 0, 0), dxfattribs={"layer": "E-RANDOM"})
    msp.add_circle((0, 0), 1, dxfattribs={"layer": "E-NOTE"})

    block = doc.blocks.new("EQ-PANEL-A")
    block.add_line((0, 0, 0), (2, 0, 0), dxfattribs={"layer": "E-TRAY-MAIN"})
    ref = msp.add_blockref(
        "EQ-PANEL-A",
        (8, 9, 1.25),
        dxfattribs={"layer": "E-EQ", "rotation": 45, "xscale": 2, "yscale": 3, "zscale": 4},
    )
    ref.add_attrib("ID", "P-01", (8, 10, 1.25), dxfattribs={"height": 1.2, "rotation": 45})
    ref.add_attrib("LOC", "R1", (8, 11, 1.25))
    msp.add_blockref("WIDGET-1", (0, 0, 0), dxfattribs={"layer": "E-EQ"})

    doc.paperspace().add_line((0, 0, 0), (9, 0, 0), dxfattribs={"layer": "E-TRAY-MAIN"})
    return save(doc, path)


def tray_width(path, insunits, width):
    doc = blank(insunits=insunits, insbase=(0, 0, 0))
    doc.modelspace().add_lwpolyline(
        [(0, 0, width, width, 0), (10, 0, width, width, 0)],
        format="xyseb",
        dxfattribs={"layer": "E-TRAY-MAIN", "elevation": 1},
    )
    return save(doc, path)
