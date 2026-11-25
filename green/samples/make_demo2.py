"""Generate Demo 2: Harbourline Residences — a synthetic 6-storey
residential block IFC, built with IfcOpenShell so GreenForge has a
second *building* demo alongside Northbank (office).

Run:  python make_demo2.py   ->  Harbourline-Residences.ifc
The model is deliberately simple massing: floor slabs, perimeter
walls, glazing panels, columns, a flat roof + covering. It exists so
the viewer and the agent loop have a real second building to work on;
it is sample data, not a design model.
"""
import ifcopenshell
from ifcopenshell import guid

W, D, FLOORS, FH = 30.0, 18.0, 6, 3.2  # width, depth, storeys, floor height
WALL_T = 0.3

m = ifcopenshell.file(schema="IFC4")
project = m.create_entity("IfcProject", GlobalId=guid.new(), Name="Harbourline Residences")
units = m.create_entity(
    "IfcUnitAssignment",
    Units=[m.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")],
)
project.UnitsInContext = units
ctx = m.create_entity(
    "IfcGeometricRepresentationContext",
    ContextType="Model",
    CoordinateSpaceDimension=3,
    Precision=1e-5,
    WorldCoordinateSystem=m.create_entity(
        "IfcAxis2Placement3D",
        Location=m.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
    ),
)
body = m.create_entity(
    "IfcGeometricRepresentationSubContext",
    ContextIdentifier="Body",
    ContextType="Model",
    ParentContext=ctx,
    TargetView="MODEL_VIEW",
)
site = m.create_entity("IfcSite", GlobalId=guid.new(), Name="Harbourline Site")
building = m.create_entity("IfcBuilding", GlobalId=guid.new(), Name="Harbourline Residences")
m.create_entity(
    "IfcRelAggregates", GlobalId=guid.new(), RelatingObject=project, RelatedObjects=[site]
)
m.create_entity(
    "IfcRelAggregates", GlobalId=guid.new(), RelatingObject=site, RelatedObjects=[building]
)
storeys = []
for i in range(FLOORS):
    st = m.create_entity(
        "IfcBuildingStorey",
        GlobalId=guid.new(),
        Name=f"Level {i + 1}",
        Elevation=i * FH,
    )
    storeys.append(st)
m.create_entity(
    "IfcRelAggregates",
    GlobalId=guid.new(),
    RelatingObject=building,
    RelatedObjects=storeys,
)


def box_solid(cx, cy, w, d, z, h):
    """An axis-aligned box: rectangle profile (w x d) centred at (cx, cy),
    extruded from z by h."""
    profile = m.create_entity(
        "IfcRectangleProfileDef",
        ProfileType="AREA",
        XDim=w,
        YDim=d,
        Position=m.create_entity(
            "IfcAxis2Placement2D",
            Location=m.create_entity("IfcCartesianPoint", Coordinates=(cx, cy)),
        ),
    )
    return m.create_entity(
        "IfcExtrudedAreaSolid",
        SweptArea=profile,
        Position=m.create_entity(
            "IfcAxis2Placement3D",
            Location=m.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, z)),
        ),
        ExtrudedDirection=m.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0)),
        Depth=h,
    )


def add(ifc_class, name, storey, solid):
    rep = m.create_entity(
        "IfcShapeRepresentation",
        ContextOfItems=body,
        RepresentationIdentifier="Body",
        RepresentationType="SweptSolid",
        Items=[solid],
    )
    el = m.create_entity(
        ifc_class,
        GlobalId=guid.new(),
        Name=name,
        Representation=m.create_entity(
            "IfcProductDefinitionShape", Representations=[rep]
        ),
    )
    m.create_entity(
        "IfcRelContainedInSpatialStructure",
        GlobalId=guid.new(),
        RelatingStructure=storey,
        RelatedElements=[el],
    )
    return el


counts = {}
for i, st in enumerate(storeys):
    z = i * FH
    # floor slab
    add("IfcSlab", f"Slab L{i + 1}", st, box_solid(0, 0, W, D, z, 0.25))
    counts["IfcSlab"] = counts.get("IfcSlab", 0) + 1
    # perimeter walls: N / S (along X), E / W (along Y)
    add("IfcWall", f"Wall N L{i + 1}", st, box_solid(0, D / 2 - WALL_T / 2, W, WALL_T, z, FH))
    add("IfcWall", f"Wall S L{i + 1}", st, box_solid(0, -D / 2 + WALL_T / 2, W, WALL_T, z, FH))
    add("IfcWall", f"Wall E L{i + 1}", st, box_solid(W / 2 - WALL_T / 2, 0, WALL_T, D, z, FH))
    add("IfcWall", f"Wall W L{i + 1}", st, box_solid(-W / 2 + WALL_T / 2, 0, WALL_T, D, z, FH))
    counts["IfcWall"] = counts.get("IfcWall", 0) + 4
    # glazing panels: 6 on each long facade, 3 on each short facade
    for k in range(6):
        x = -W / 2 + 3.0 + k * 4.8
        add("IfcWindow", f"Win N L{i + 1}-{k + 1}", st, box_solid(x, D / 2 + 0.03, 2.4, 0.08, z + 0.9, 1.5))
        add("IfcWindow", f"Win S L{i + 1}-{k + 1}", st, box_solid(x, -D / 2 - 0.03, 2.4, 0.08, z + 0.9, 1.5))
    for k in range(3):
        y = -D / 2 + 3.0 + k * 6.0
        add("IfcWindow", f"Win E L{i + 1}-{k + 1}", st, box_solid(W / 2 + 0.03, y, 0.08, 2.4, z + 0.9, 1.5))
        add("IfcWindow", f"Win W L{i + 1}-{k + 1}", st, box_solid(-W / 2 - 0.03, y, 0.08, 2.4, z + 0.9, 1.5))
    counts["IfcWindow"] = counts.get("IfcWindow", 0) + 18
    # corner + mid columns
    for (cx, cy) in [(W / 2 - 0.4, D / 2 - 0.4), (-W / 2 + 0.4, D / 2 - 0.4),
                     (W / 2 - 0.4, -D / 2 + 0.4), (-W / 2 + 0.4, -D / 2 + 0.4),
                     (0, D / 2 - 0.4), (0, -D / 2 + 0.4)]:
        add("IfcColumn", f"Col L{i + 1}", st, box_solid(cx, cy, 0.4, 0.4, z, FH))
    counts["IfcColumn"] = counts.get("IfcColumn", 0) + 6

top = storeys[-1]
ztop = FLOORS * FH
add("IfcRoof", "Flat roof", top, box_solid(0, 0, W + 0.6, D + 0.6, ztop, 0.3))
add("IfcCovering", "Roof covering", top, box_solid(0, 0, W + 0.6, D + 0.6, ztop + 0.3, 0.06))
counts["IfcRoof"] = 1
counts["IfcCovering"] = 1

out = "Harbourline-Residences.ifc"
m.write(out)
print("wrote", out, counts)
