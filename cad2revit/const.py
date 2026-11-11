"""Schema names shared by the mapper, the writer, and the interface spec."""

TOOL_NAME = "cad2revit"

MAPPING_SCHEMA = "cad2revit.mapping.v1"
ELEMENTS_SCHEMA = "cad2revit.elements.v1"
BATCH_SCHEMA = "cad2revit.batch.v1"

DRAWING_SUFFIXES = (".dwg", ".dxf")

# Parameter unit names accepted in a mapping file. Lengths are converted into
# this unit only when the drawing's $INSUNITS is a known length.
PARAMETER_UNITS = ("mm", "cm", "m", "in", "ft")

# Categories whose placement size cannot be left empty. Other category names
# are passed through; this is not a closed list of Revit categories.
SIZE_REQUIREMENTS = {
    "Cable Tray": ("width", "height"),
    "Conduit": ("diameter",),
}

LINEAR_TYPES = ("LINE", "LWPOLYLINE", "POLYLINE", "ARC")
ANNOTATION_TYPES = ("TEXT", "MTEXT")
