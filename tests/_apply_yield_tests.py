"""One-shot test update: P3d neighbor-yield section before P4."""
import io
import os
import sys

P = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "test_refboard.py")

NEW = '''    # ------------------------------------------------------------------
    section("P3d neighbor yield on scale")
    # Pinned 800x600 canvas. item (idx 0) sits at center spanning x 350-450;
    # a neighbor at 0.7 spans x 510-610, 60 px clear.
    item.pos = (0.5, 0.5)
    item.scale = (1.0, 1.0)
    item.rotation = 0.0
    item.crop = (0.0, 0.0, 1.0, 1.0)
    yb = scene.refboard_items.add()
    yb.image = big
    yb.pos = (0.7, 0.5)
    yb.scale = (1.0, 1.0)
    yb.rotation = 0.0
    yb.crop = (0.0, 0.0, 1.0, 1.0)
    yb_idx = len(scene.refboard_items) - 1
    cv = ct._refboard_canvas_view(scene, reg)
    quad_of = lambda it: ct._refboard_quad(
        ct._refboard_rect(it, reg, big, cv))

    # Grow the intruder to 3.4x: x spans 230-570, overlapping b by 60 px.
    ct._refboard_yield_begin(scene, {0})
    item.scale = (3.4, 3.4)
    iquads = [quad_of(item)]
    check("intruder overlaps neighbor",
          ct._refboard_sat_mtv(quad_of(yb), iquads[0]) is not None)

    # One step lerps partway - not a snap to the resolved target.
    ct._refboard_yield_solve(scene, reg, iquads, dt=1.0 / 60.0)
    s1 = yb.scale[0]
    check("yield lerps, not snaps", 0.02 < 1.0 - s1 < 0.95,
          "scale=%.4f" % s1)

    # Converged: clear of the intruder, shrunk, and nudged away.
    for _ in range(600):
        if not ct._refboard_yield_solve(scene, reg, iquads,
                                        dt=1.0 / 60.0):
            break
    check("settled clear of intruder",
          ct._refboard_sat_mtv(quad_of(yb), iquads[0]) is None)
    check("neighbor shrank", yb.scale[0] < 0.9, "scale=%.4f" % yb.scale[0])
    check("neighbor moved aside", yb.pos[0] > 0.7 + 1e-4,
          "pos=%.4f" % yb.pos[0])
    check("not crushed below floor",
          yb.scale[0] >= 1.0 * ct._REFBOARD_YIELD_MIN - 1e-3)

    # Release snaps whatever residual is left and clears the state.
    ct._refboard_yield_end(scene, reg)
    check("yield end clears state", ct._refboard_yield is None)
    check("end state still clear",
          ct._refboard_sat_mtv(quad_of(yb), iquads[0]) is None)

    # Reversing the intrusion grows the neighbor back toward base.
    ct._refboard_yield_begin(scene, {0})
    item.scale = (1.0, 1.0)
    for _ in range(600):
        if not ct._refboard_yield_solve(scene, reg, [], dt=1.0 / 60.0):
            break
    check("eases back toward base",
          abs(yb.scale[0] - 1.0) < 0.03 and abs(yb.pos[0] - 0.7) < 0.02,
          "scale=%.4f pos=%.4f" % (yb.scale[0], yb.pos[0]))
    ct._refboard_yield_end(scene, reg)

    # Cancel (Esc) flags restore mode instead of snapping.
    ct._refboard_yield_begin(scene, {0})
    item.scale = (3.4, 3.4)
    iquads = [quad_of(item)]
    for _ in range(120):
        ct._refboard_yield_solve(scene, reg, iquads, dt=1.0 / 60.0)
    ct._refboard_yield_end(scene, reg, cancel=True)
    check("cancel keeps yield easing", ct._refboard_yield is not None and
          ct._refboard_yield["restore"])
    item.scale = (1.0, 1.0)
    for _ in range(600):
        if not ct._refboard_yield_solve(scene, reg, [], dt=1.0 / 60.0):
            break
    check("cancel eased back to base",
          abs(yb.scale[0] - 1.0) < 0.03, "scale=%.4f" % yb.scale[0])
    check("restore settles and clears", ct._refboard_yield is None)

    # Locked neighbors never yield.
    yb.locked = True
    ct._refboard_yield_begin(scene, {0})
    item.scale = (3.4, 3.4)
    for _ in range(60):
        ct._refboard_yield_solve(scene, reg, [quad_of(item)],
                                 dt=1.0 / 60.0)
    check("locked neighbor does not yield",
          abs(yb.scale[0] - 1.0) < 1e-4 and abs(yb.pos[0] - 0.7) < 1e-6)
    ct._refboard_yield_end(scene, reg)
    yb.locked = False
    item.scale = (1.0, 1.0)
    scene.refboard_items.remove(yb_idx)

'''

OLD = '''    # ------------------------------------------------------------------
    section("P4 drag math")
'''

with io.open(P, "r", encoding="utf8", newline="") as f:
    src = f.read()

# The target file is CRLF; normalize the literals to match.
old = OLD.replace("\n", "\r\n")
new = NEW.replace("\n", "\r\n")

if src.count(old) != 1:
    sys.exit("ABORT: anchor count %d" % src.count(old))
src = src.replace(old, new + old)

with io.open(P, "w", encoding="utf8", newline="") as f:
    f.write(src)

print("OK")
