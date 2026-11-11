"""Phase 1 of the DWG → Revit auto-modeling engine.

The package reads DWG (via the repo's existing accoreconsole converter) or DXF,
classifies geometry with a mapping file, and writes JSON and CSV for a later
placement step. It does not place objects in Revit, does not build a Dynamo
graph, and does not modify source drawings.
"""

__version__ = "0.1.0"
