"""
Headless tests for the Refboard clipboard-image feature.

Run:
    blender --background --factory-startup --python tests/test_refboard.py

Covers:
  * screen-space finish path adds a normalized-pos item with autofit scale
  * 3d-space finish path creates a cardinal-aligned image empty
  * overlay geometry math (rect / local / quad / zone picking)
  * drag math (move, corner scale, edge scale, rotate, crop, center scale)
  * persistence through save + reload
  * selection delete and clear
  * clipboard image gate

GPU drawing and modal event flow are not testable in -b (no GPU context).
Prints PASS/FAIL per check and exits non-zero on any failure.
"""

import math as _m
import os
import sys
import tempfile
import time
import types
import uuid

import bpy

PASS_COUNT = 0
FAIL_COUNT = 0
SECTION = ""


def check(name, cond, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
    else:
        FAIL_COUNT += 1
    line = f"[{'PASS' if cond else 'FAIL'}] {SECTION}: {name}"
    if detail:
        line += f"  -- {detail}"
    print(line, flush=True)


def section(title):
    global SECTION
    SECTION = title
    print(f"\n=== {title} ===", flush=True)


def make_png(path, w=8, h=8, unique=True):
    """Write a test PNG. Content varies per path by default, because the addon
    stores images content-addressed: identical bytes deliberately collapse to a
    single repo file and a single datablock. Pass unique=False to exercise that.
    """
    img = bpy.data.images.new("png_src", w, h, alpha=True)
    tint = (hash(os.path.basename(path)) % 97) / 97.0 if unique else 0.2
    img.pixels[:] = [tint, 0.6, 1.0, 1.0] * (w * h)
    img.filepath_raw = path
    img.file_format = 'PNG'
    try:
        img.save()
    except Exception:
        img.save_render(path)
    bpy.data.images.remove(img)
    return path


def fake_region(w=800, h=600):
    return types.SimpleNamespace(width=w, height=h,
                                 as_pointer=lambda: 1)


def main():
    ctx = bpy.context
    scene = ctx.scene

    bpy.ops.preferences.addon_enable(module="refboard")
    import refboard as ct
    if "refboard" not in ctx.preferences.addons:
        print("FATAL: refboard addon could not be enabled", flush=True)
        return 1

    tmp = tempfile.mkdtemp(prefix="refboard_test_")

    # ------------------------------------------------------------------
    section("P1 screen-space finish adds overlay item")
    png = make_png(os.path.join(tmp, "p1.png"))
    ct._refboard_finish({
        "dst": png, "mode": 'SCREEN',
        "state": {"pos": (0.25, 0.75), "rw": 800, "rh": 600,
                  "scene": scene},
    })
    check("one item", len(scene.refboard_items) == 1)
    item = scene.refboard_items[0]
    check("auto name img.001", item.name == "img.001", item.name)
    # next item gets img.002
    ct._refboard_finish({
        "dst": png, "mode": 'SCREEN',
        "state": {"pos": (0.5, 0.5), "rw": 800, "rh": 600, "scene": scene},
    })
    check("auto name img.002", scene.refboard_items[1].name == "img.002",
          scene.refboard_items[1].name)
    scene.refboard_items.remove(1)
    scene.refboard_selected = 0
    check("image not packed", item.image is not None and
          item.image.packed_file is None)
    # Unsaved file here, so the repo has no home yet and the cache holds it.
    holder = os.path.basename(os.path.dirname(
        bpy.path.abspath(item.image.filepath_raw))) if item.image else ""
    check("image stored outside the blend",
          holder in ("refboard", "refboard_unsaved"), holder)
    check("stored name is a content hash",
          item.image is not None and
          len(os.path.splitext(
              os.path.basename(item.image.filepath_raw))[0]) == 16,
          item.image.filepath_raw if item.image else "")

    # Content addressing: the same bytes must reuse one file and one datablock.
    dup_a = make_png(os.path.join(tmp, "dup_a.png"), unique=False)
    dup_b = make_png(os.path.join(tmp, "dup_b.png"), unique=False)
    n_before = len(bpy.data.images)
    ct._refboard_finish({"dst": dup_a, "mode": 'SCREEN',
                         "state": {"pos": (0.5, 0.5), "rw": 800, "rh": 600,
                                   "scene": scene}})
    mid = len(bpy.data.images)
    ct._refboard_finish({"dst": dup_b, "mode": 'SCREEN',
                         "state": {"pos": (0.5, 0.5), "rw": 800, "rh": 600,
                                   "scene": scene}})
    check("identical images add one datablock",
          len(bpy.data.images) == mid and mid == n_before + 1,
          "%d -> %d -> %d" % (n_before, mid, len(bpy.data.images)))
    a_img = scene.refboard_items[-2].image
    b_img = scene.refboard_items[-1].image
    check("identical images share one repo file",
          a_img == b_img and
          a_img.filepath_raw == b_img.filepath_raw,
          a_img.filepath_raw)
    while len(scene.refboard_items) > 1:
        scene.refboard_items.remove(len(scene.refboard_items) - 1)
    scene.refboard_selected = 0
    check("pos stored", abs(item.pos[0] - 0.25) < 1e-6 and
          abs(item.pos[1] - 0.75) < 1e-6)
    check("autofit scale <= 1", item.scale[0] <= 1.0 and item.scale[0] > 0)
    check("selected after paste", scene.refboard_selected == 0)
    check("crop defaults full", tuple(item.crop) == (0.0, 0.0, 1.0, 1.0))

    # ------------------------------------------------------------------
    section("P2 3d-space finish creates image empty")
    png2 = make_png(os.path.join(tmp, "p2.png"))
    n_obj = len(scene.objects)
    ct._refboard_finish({
        "dst": png2, "mode": '3D',
        "state": {"pos": (0.5, 0.5), "scene": scene,
                  "collection": scene.collection,
                  "basis": None},
    })
    check("object added", len(scene.objects) == n_obj + 1)
    emp = next((o for o in scene.objects if o.name.startswith("Refboard")
                and o.type == 'EMPTY'), None)
    check("empty created", emp is not None)
    check("empty is image type",
          emp is not None and emp.empty_display_type == 'IMAGE')
    check("empty has image", emp is not None and emp.data is not None)

    # Drag-drop path: menu state with a filepath goes straight to finish,
    # no clipboard subprocess. Item keeps the file's own image name.
    png_drop = make_png(os.path.join(tmp, "dropped.png"))
    n_items = len(scene.refboard_items)
    ct._refboard_menu_state = {
        "filepath": png_drop, "pos": (0.6, 0.4), "rw": 800, "rh": 600,
        "scene": scene,
    }
    try:
        bpy.ops.refboard.do_paste(mode='SCREEN')
        check("drop adds item", len(scene.refboard_items) == n_items + 1)
        ditem = scene.refboard_items[n_items]
        check("drop pos", abs(ditem.pos[0] - 0.6) < 1e-6 and
              abs(ditem.pos[1] - 0.4) < 1e-6)
        check("drop image external",
              ditem.image is not None and
              ditem.image.packed_file is None)
        scene.refboard_items.remove(n_items)
    except Exception as e:
        check("drop adds item", False, repr(e))
    check("file handler registered",
          hasattr(bpy.types, "REFBOARD_FH_image"))

    # ------------------------------------------------------------------
    section("P3 overlay geometry and zone picking")
    reg = fake_region(800, 600)
    item.pos = (0.5, 0.5)
    item.scale = (1.0, 1.0)
    item.rotation = 0.0
    item.crop = (0.0, 0.0, 1.0, 1.0)
    # Give the item a 100x50 logical image for predictable math.
    big = bpy.data.images.new("big", 100, 50)
    item.image = big
    rect = ct._refboard_rect(item, reg, big)
    check("rect center at pos", rect is not None and
          abs(rect[4] - 400) < 1e-6 and abs(rect[5] - 300) < 1e-6)
    check("rect half extents", abs(rect[2] - 50) < 1e-6 and
          abs(rect[3] - 25) < 1e-6)
    quad = ct._refboard_quad(rect)
    check("quad bl corner", abs(quad[0][0] - 350) < 1e-4 and
          abs(quad[0][1] - 275) < 1e-4)
    # round trip
    lx, ly = ct._refboard_to_local(rect, 450, 325)
    sx, sy = ct._refboard_to_screen(rect, lx, ly)
    check("local/screen round trip", abs(sx - 450) < 1e-4 and
          abs(sy - 325) < 1e-4)
    z = ct._refboard_zone(rect, 400, 300, True)
    check("inside zone", z == ('inside', -1), str(z))
    z = ct._refboard_zone(rect, 352, 277, True)
    check("corner zone", z == ('corner', 0), str(z))
    z = ct._refboard_zone(rect, 400, 275, True)
    check("edge dot zone", z == ('edge', 0), str(z))
    # Rotate band: derived from the constants so retuning the marker size
    # does not need the expected pixels rewritten here.
    mid = (ct._REFBOARD_ROT_IN + ct._REFBOARD_ROT_OUT) * 0.5 / _m.sqrt(2.0)
    z = ct._refboard_zone(rect, 350.0 - mid, 275.0 - mid, True)
    check("rotate zone", z == ('rotate', 0), str(z))
    # Just inside ROT_IN is a dead ring: no rotate while touching the quad.
    dead = (ct._REFBOARD_ROT_IN - 4.0) * 0.5 / _m.sqrt(2.0)
    z = ct._refboard_zone(rect, 350.0 - dead, 275.0 - dead, True)
    check("rotate dead ring", z is None, str(z))
    check("marker sits inside the rotate band",
          ct._REFBOARD_ROT_IN < ct._REFBOARD_ROT_R < ct._REFBOARD_ROT_OUT,
          "in=%.1f r=%.1f out=%.1f" % (ct._REFBOARD_ROT_IN,
                                       ct._REFBOARD_ROT_R,
                                       ct._REFBOARD_ROT_OUT))
    # Crop edges only exist in Ctrl crop mode; without it the border is
    # just the image interior.
    z = ct._refboard_zone(rect, 450, 290, True, True)
    check("border crop zone", z == ('crop', 2), str(z))
    z = ct._refboard_zone(rect, 450, 290, True)
    check("no crop without ctrl", z == ('inside', -1), str(z))
    # In crop mode the scale dots are suppressed.
    z = ct._refboard_zone(rect, 352, 277, True, True)
    check("no dot in crop mode", z != ('corner', 0), str(z))
    z = ct._refboard_zone(rect, 341.5, 266.5, False)
    check("no handles unselected", z is None, str(z))
    z = ct._refboard_zone(rect, 700, 100, True)
    check("outside is none", z is None, str(z))



    # pick returns the selected item's zones first
    scene.refboard_selected = 0
    hit = ct._refboard_pick(scene, reg, 352, 277)
    check("pick finds corner", hit == (0, 'corner', 0), str(hit))
    hit = ct._refboard_pick(scene, reg, 700, 100)
    check("pick miss", hit is None, str(hit))

    # ------------------------------------------------------------------
    section("P3b canvas view transform")
    item.pos = (0.5, 0.5)
    item.scale = (1.0, 1.0)
    item.rotation = 0.0
    item.crop = (0.0, 0.0, 1.0, 1.0)
    scene.refboard_view_zoom = 2.0
    scene.refboard_view_pan = (0.1, 0.0)
    vrect = ct._refboard_rect(item, reg, big,
                              ct._refboard_view(scene, reg))
    # center: ((0.5-0.5)*2 + 0.5 + 0.1)*800 = 480 ; y: 300
    check("zoomed rect center", vrect is not None and
          abs(vrect[4] - 480) < 1e-4 and abs(vrect[5] - 300) < 1e-4,
          str(vrect))
    check("zoomed rect extents", abs(vrect[2] - 100) < 1e-4 and
          abs(vrect[3] - 50) < 1e-4)
    # screen<->canvas roundtrip
    cx, cy = ct._refboard_to_canvas_px(reg, scene, 480, 300)
    check("canvas roundtrip", abs(cx - 400) < 1e-4 and abs(cy - 300) < 1e-4,
          str((cx, cy)))
    # zoom-to-cursor invariance: canvas point under the mouse stays fixed
    sx0, sy0 = 640.0, 420.0
    kx = (sx0 / 800 - 0.5 - 0.1) / 2.0 + 0.5
    ky = (sy0 / 600 - 0.5 - 0.0) / 2.0 + 0.5
    z2 = 3.2
    pan2 = (sx0 / 800 - 0.5 - (kx - 0.5) * z2,
            sy0 / 600 - 0.5 - (ky - 0.5) * z2)
    scene.refboard_view_zoom = z2
    scene.refboard_view_pan = pan2
    cx2, cy2 = ct._refboard_to_canvas_px(reg, scene, sx0, sy0)
    check("zoom anchor fixed", abs(cx2 / 800 - kx) < 1e-5 and
          abs(cy2 / 600 - ky) < 1e-5, str((cx2, cy2)))
    # pick still works under transform: br corner at 480+100*? recompute
    scene.refboard_view_zoom = 2.0
    scene.refboard_view_pan = (0.1, 0.0)
    scene.refboard_selected = 0
    hit = ct._refboard_pick(scene, reg, 580, 250)
    check("pick under zoom", hit == (0, 'corner', 1), str(hit))
    # paste: invoke converts the drop point to canvas space up front (the
    # same to_canvas_px call the operators make), so `pos` arrives already
    # in canvas units and lands verbatim.
    cmx, cmy = ct._refboard_to_canvas_px(reg, scene, 0.9 * 800, 0.5 * 600)
    png3 = make_png(os.path.join(tmp, "p3b.png"))
    ct._refboard_finish({
        "dst": png3, "mode": 'SCREEN',
        "state": {"pos": (cmx / 800.0, cmy / 600.0),
                  "rw": 800, "rh": 600, "scene": scene},
    })
    pit = scene.refboard_items[len(scene.refboard_items) - 1]
    # canvas x = (0.9 - 0.5 - 0.1)/2 + 0.5 = 0.65
    check("paste lands under cursor at zoom",
          abs(pit.pos[0] - 0.65) < 1e-4, str(tuple(pit.pos)))
    scene.refboard_items.remove(len(scene.refboard_items) - 1)
    scene.refboard_view_zoom = 1.0
    scene.refboard_view_pan = (0.0, 0.0)

    # ------------------------------------------------------------------
    section("P3c viewport resize invariance")
    # Canvas pinned to 800x600 (by the first paste). A wider viewport must
    # re-center only; a taller one scales positions and sizes together.
    item.pos = (0.3, 0.5)
    item.scale = (1.0, 1.0)
    item.rotation = 0.0
    item.crop = (0.0, 0.0, 1.0, 1.0)
    scene.refboard_view_zoom = 1.0
    scene.refboard_view_pan = (0.0, 0.0)

    reg_wide = fake_region(1600, 600)
    wrect = ct._refboard_rect(item, reg_wide, big,
                              ct._refboard_view(scene, reg_wide))
    # k=1 (height unchanged): extents unchanged; center re-centers.
    # pan_eff_x = 1600/2/800 - 0.5 = 0.5 -> px = (0.3 + 0.5)*800 = 640
    check("wide resize recenters center",
          wrect is not None and abs(wrect[4] - 640) < 1e-4 and
          abs(wrect[5] - 300) < 1e-4, str(wrect))
    check("wide resize keeps pixel size",
          abs(wrect[2] - 50) < 1e-4 and abs(wrect[3] - 25) < 1e-4)

    reg_tall = fake_region(800, 1200)
    trect = ct._refboard_rect(item, reg_tall, big,
                              ct._refboard_view(scene, reg_tall))
    # k=2: positions and extents both double -> similarity, no shear.
    check("tall resize scales size uniformly",
          trect is not None and abs(trect[2] - 100) < 1e-4 and
          abs(trect[3] - 50) < 1e-4, str(trect))

    # Relative spacing: a second ref at pos.x 0.5. Canvas gap between
    # centers = 160 px; on screen it must scale by exactly the same factor
    # the image extents did, so gap/size ratio is resize-invariant.
    item2 = scene.refboard_items.add()
    item2.image = big
    item2.pos = (0.5, 0.5)
    item2.scale = (1.0, 1.0)
    item2.rotation = 0.0
    item2.crop = (0.0, 0.0, 1.0, 1.0)
    r1a = ct._refboard_rect(item, reg, big, ct._refboard_view(scene, reg))
    r1b = ct._refboard_rect(item2, reg, big,
                            ct._refboard_view(scene, reg))
    r2a = ct._refboard_rect(item, reg_tall, big,
                            ct._refboard_view(scene, reg_tall))
    r2b = ct._refboard_rect(item2, reg_tall, big,
                            ct._refboard_view(scene, reg_tall))
    gap1 = r1b[4] - r1a[4]
    gap2 = r2b[4] - r2a[4]
    check("spacing scales with size",
          abs(gap1 - 160.0) < 1e-3 and abs(gap2 - 320.0) < 1e-3,
          "%.3f -> %.3f" % (gap1, gap2))
    check("spacing/size ratio invariant",
          abs(gap1 / (r1a[2] * 2.0) - gap2 / (r2a[2] * 2.0)) < 1e-4)
    # Wide-only: same pixel gap, same pixel size - only recentered.
    r3a = ct._refboard_rect(item, reg_wide, big,
                            ct._refboard_view(scene, reg_wide))
    r3b = ct._refboard_rect(item2, reg_wide, big,
                            ct._refboard_view(scene, reg_wide))
    check("wide-only keeps gap in px",
          abs((r3b[4] - r3a[4]) - gap1) < 1e-3)
    scene.refboard_items.remove(len(scene.refboard_items) - 1)

    # Drag math is canvas-relative: the same 80 px drag moves pos by
    # 80/800 of the canvas, not of the (now 1600-wide) viewport.
    item.pos = (0.3, 0.5)
    ct._refboard_drag_set({
        "index": 0, "mode": 'inside', "sub": -1, "temp": False,
        "moved": False,
        "m0": ct._refboard_to_canvas_px(reg_wide, scene, 720, 300),
        "snap": ct._refboard_snapshot(item),
        "pos0": tuple(item.pos), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (0.0, 0.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg_wide, 800, 300)
    check("drag delta uses canvas width",
          abs(item.pos[0] - (0.3 + 80.0 / 800.0)) < 1e-4,
          str(tuple(item.pos)))
    ct._refboard_drag_set(None)

    # Zoom anchor pins the canvas point under the cursor at any viewport.
    cmx, cmy = ct._refboard_to_canvas_px(reg_tall, scene, 200, 900)
    ct._refboard_zoom_anchor(scene, reg_tall, 200, 900, cmx, cmy, 3.0)
    rx, ry = ct._refboard_to_canvas_px(reg_tall, scene, 200, 900)
    check("zoom anchor holds under resize",
          abs(rx - cmx) < 1e-3 and abs(ry - cmy) < 1e-3,
          "%.3f,%.3f vs %.3f,%.3f" % (rx, ry, cmx, cmy))
    scene.refboard_view_zoom = 1.0
    scene.refboard_view_pan = (0.0, 0.0)

    # Reference-size lifecycle, on a throwaway scene so the real items
    # stay valid: unpinned falls back to the live region (old-file
    # compat), an empty board never pins, first contact pins, and a pin
    # is sticky across later viewport sizes.
    tscene = bpy.data.scenes.new("refboard_rs_test")
    try:
        check("unpinned ref falls back to region",
              ct._refboard_ref_size(tscene, reg_wide) == (1600.0, 600.0))
        ct._refboard_ensure_ref_size(tscene, reg_wide)
        check("empty board does not pin",
              tuple(tscene.refboard_ref_size) == (0.0, 0.0))
        tscene.refboard_items.add().image = big
        ct._refboard_ensure_ref_size(tscene, reg_wide)
        check("first contact pins canvas",
              tuple(tscene.refboard_ref_size) == (1600.0, 600.0),
              str(tuple(tscene.refboard_ref_size)))
        ct._refboard_ensure_ref_size(tscene, reg)
        check("pin is sticky",
              tuple(tscene.refboard_ref_size) == (1600.0, 600.0))

        # A pin below 64px can only be a mid-resize transient: ref_size
        # ignores it and the next ensure re-pins to the settled size.
        tscene.refboard_ref_size = (20.0, 20.0)
        check("corrupt tiny pin is ignored",
              ct._refboard_ref_size(tscene, reg_wide) == (1600.0, 600.0))
        ct._refboard_ensure_ref_size(tscene, reg_wide)
        check("corrupt pin heals on next gesture",
              tuple(tscene.refboard_ref_size) == (1600.0, 600.0))

        # Transient mid-resize sizes never become the canvas: too small,
        # and ambient (stable) calls need the size to hold for a moment.
        tscene.refboard_ref_size = (0.0, 0.0)
        ct._refboard_ensure_ref_size(tscene, fake_region(40, 30))
        ct._refboard_ensure_ref_size(tscene, fake_region(40, 30),
                                     stable=True)
        check("tiny transient never pins",
              tuple(tscene.refboard_ref_size) == (0.0, 0.0))
        ct._refboard_ensure_ref_size(tscene, reg_tall, stable=True)
        check("first ambient sighting does not commit",
              tuple(tscene.refboard_ref_size) == (0.0, 0.0))
        ct._refboard_ref_pending[tscene.as_pointer()] = (
            800.0, 1200.0, time.time() - 1.0)
        ct._refboard_ensure_ref_size(tscene, reg_tall, stable=True)
        check("stable size commits after the settle window",
              tuple(tscene.refboard_ref_size) == (800.0, 1200.0))

        # Even a bad-but-plausible pin can't blow the layout up: the fit
        # factor is clamped both ways.
        tscene.refboard_ref_size = (100.0, 100.0)
        check("fit clamps the blow-up",
              ct._refboard_fit(tscene, reg_tall) == 8.0,
              str(ct._refboard_fit(tscene, reg_tall)))
        check("fit clamps the collapse",
              ct._refboard_fit(tscene, fake_region(100, 2)) == 0.125)
    finally:
        ct._refboard_ref_pending.pop(tscene.as_pointer(), None)
        bpy.data.scenes.remove(tscene)
    item.pos = (0.5, 0.5)

    # ------------------------------------------------------------------
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

    # Mid-gesture reversal is where the fluidity shows: shrinking the
    # intruder back eases the neighbor toward its pre-drag state, not to
    # wherever it happens to be now.
    item.scale = (1.0, 1.0)
    for _ in range(600):
        if not ct._refboard_yield_solve(scene, reg, [], dt=1.0 / 60.0):
            break
    check("eases back toward base",
          abs(yb.scale[0] - 1.0) < 0.03 and abs(yb.pos[0] - 0.7) < 0.02,
          "scale=%.4f pos=%.4f" % (yb.scale[0], yb.pos[0]))

    # Grow it again and release: the snap converges and stays clear. The
    # real release path calls yield_end while the drag dict is still live,
    # so the intruder quads still resolve - mirror that here.
    item.scale = (3.4, 3.4)
    iquads = [quad_of(item)]
    for _ in range(600):
        if not ct._refboard_yield_solve(scene, reg, iquads,
                                        dt=1.0 / 60.0):
            break
    ct._refboard_drag_set({"index": 0, "mode": 'corner', "sub": 0,
                           "group_members": None})
    ct._refboard_yield_end(scene, reg)
    ct._refboard_drag_set(None)
    check("yield end clears state", ct._refboard_yield is None)
    check("end state still clear",
          ct._refboard_sat_mtv(quad_of(yb), iquads[0]) is None)

    # Cancel (Esc) flags restore mode: the neighbor eases back to THIS
    # gesture's start state (each gesture re-bases on begin), not to the
    # resolved post-drag layout.
    yb.scale = (1.0, 1.0)
    yb.pos = (0.7, 0.5)
    cs0, cp0 = yb.scale[0], yb.pos[0]
    ct._refboard_yield_begin(scene, {0})
    item.scale = (3.4, 3.4)
    iquads = [quad_of(item)]
    for _ in range(120):
        ct._refboard_yield_solve(scene, reg, iquads, dt=1.0 / 60.0)
    check("yield ran pre-cancel", yb.scale[0] < cs0 - 0.05,
          "scale=%.4f" % yb.scale[0])
    ct._refboard_yield_end(scene, reg, cancel=True)
    check("cancel keeps yield easing", ct._refboard_yield is not None and
          ct._refboard_yield["restore"])
    item.scale = (1.0, 1.0)
    for _ in range(600):
        if not ct._refboard_yield_solve(scene, reg, [], dt=1.0 / 60.0):
            break
    check("cancel eased back to gesture base",
          abs(yb.scale[0] - cs0) < 0.03 and abs(yb.pos[0] - cp0) < 0.02,
          "scale=%.4f pos=%.4f" % (yb.scale[0], yb.pos[0]))
    check("restore settles and clears", ct._refboard_yield is None)

    # Back-to-back gestures must not compound the shrink: the floor anchors
    # to the item's authored scale (home_scale), not each gesture's base.
    yb.scale = (1.0, 1.0)
    yb.pos = (0.7, 0.5)
    yb.home_scale = (0.0, 0.0)
    ct._refboard_yield_begin(scene, {0})
    item.scale = (3.4, 3.4)
    for _ in range(600):
        ct._refboard_yield_solve(scene, reg, [quad_of(item)],
                                 dt=1.0 / 60.0)
    ct._refboard_drag_set({"index": 0, "mode": 'corner', "sub": 0,
                           "group_members": None})
    ct._refboard_yield_end(scene, reg)
    ct._refboard_drag_set(None)
    floor0 = yb.scale[0]
    check("first gesture floors at authored min",
          abs(floor0 - ct._REFBOARD_YIELD_MIN) < 0.05,
          "scale=%.4f" % floor0)
    # Second push at a much bigger intruder: scale must not go below
    # MIN * home - the gesture start (already at the floor) is not the
    # reference anymore.
    ct._refboard_yield_begin(scene, {0})
    item.scale = (8.0, 8.0)
    for _ in range(600):
        ct._refboard_yield_solve(scene, reg, [quad_of(item)],
                                 dt=1.0 / 60.0)
    ct._refboard_drag_set({"index": 0, "mode": 'corner', "sub": 0,
                           "group_members": None})
    ct._refboard_yield_end(scene, reg)
    ct._refboard_drag_set(None)
    check("repeated gestures cannot compound-shrink",
          yb.scale[0] >= ct._REFBOARD_YIELD_MIN * 1.0 - 0.01,
          "scale=%.4f floor0=%.4f" % (yb.scale[0], floor0))

    # Locked neighbors never yield.
    yb.locked = True
    ls0, lp0 = yb.scale[0], yb.pos[0]
    ct._refboard_yield_begin(scene, {0})
    item.scale = (3.4, 3.4)
    for _ in range(60):
        ct._refboard_yield_solve(scene, reg, [quad_of(item)],
                                 dt=1.0 / 60.0)
    check("locked neighbor does not yield",
          abs(yb.scale[0] - ls0) < 1e-5 and abs(yb.pos[0] - lp0) < 1e-6,
          "scale=%.4f pos=%.4f" % (yb.scale[0], yb.pos[0]))
    ct._refboard_yield_end(scene, reg)
    yb.locked = False
    item.scale = (1.0, 1.0)
    scene.refboard_items.remove(yb_idx)

    # ------------------------------------------------------------------
    section("P4 drag math")
    # move
    item.pos = (0.5, 0.5); item.scale = (1.0, 1.0)
    item.rotation = 0.0; item.crop = (0.0, 0.0, 1.0, 1.0)
    ct._refboard_drag_set({
        "index": 0, "mode": 'inside', "sub": -1, "temp": False,
        "moved": False, "m0": (400, 300), "snap": ct._refboard_snapshot(item),
        "pos0": tuple(item.pos), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (0.0, 0.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 500, 360)
    check("move drag", abs(item.pos[0] - 0.625) < 1e-6 and
          abs(item.pos[1] - 0.6) < 1e-6, str(tuple(item.pos)))

    # corner scale: grab BR corner, drag to texel (100,-50) -> projection
    # factor 1.5, pivoted on the opposite (TL) corner
    item.pos = (0.5, 0.5); item.scale = (1.0, 1.0)
    ct._refboard_drag_set({
        "index": 0, "mode": 'corner', "sub": 1, "temp": False,
        "moved": False, "m0": (450, 275), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (50.0, -25.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 500, 250)  # texel (100,-50)
    check("corner uniform scale", abs(item.scale[0] - 1.5) < 1e-4 and
          abs(item.scale[1] - 1.5) < 1e-4, str(tuple(item.scale)))
    # TL corner texel (-50,25) pinned at screen (350,325) -> center moves to
    # (350+75, 325-37.5) = (425, 287.5)
    check("corner pivots opposite corner",
          abs(item.pos[0] - 425.0 / 800.0) < 1e-4 and
          abs(item.pos[1] - 287.5 / 600.0) < 1e-4, str(tuple(item.pos)))
    item.pos = (0.5, 0.5)

    # edge scale: bottom edge dot, drag down scales uniformly AND pivots
    # around the opposite (top) edge midpoint
    item.scale = (1.0, 1.0)
    item.pos = (0.5, 0.5)
    ct._refboard_drag_set({
        "index": 0, "mode": 'edge', "sub": 0, "temp": False,
        "moved": False, "m0": (400, 275), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (0.0, -25.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 400, 250)  # local (0,-50) -> f=1.5
    check("edge scale uniform", abs(item.scale[1] - 1.5) < 1e-4 and
          abs(item.scale[0] - 1.5) < 1e-4, str(tuple(item.scale)))
    # top edge mid is the pivot: anchor texel (0,25) stays at y=325 on screen
    # -> center moves to 300 - 25*1.5 = 287.5px -> 0.47917 normalized
    check("edge scale pivots opposite edge",
          abs(item.pos[1] - (287.5 / 600.0)) < 1e-4 and
          abs(item.pos[0] - 0.5) < 1e-4, str(tuple(item.pos)))

    # center_scale: horizontal rate control, +250px doubles
    item.scale = (1.0, 1.0)
    ct._refboard_drag_set({
        "index": 0, "mode": 'center_scale', "sub": -1, "temp": False,
        "moved": False, "m0": (400, 300), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (0.0, 0.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 650, 300)
    check("ctrl-alt right drag doubles", abs(item.scale[0] - 2.0) < 1e-4,
          str(tuple(item.scale)))
    ct._refboard_drag_set({
        "index": 0, "mode": 'center_scale', "sub": -1, "temp": False,
        "moved": False, "m0": (650, 300), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (0.0, 0.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 400, 300)
    check("ctrl-alt left drag halves", abs(item.scale[0] - 0.5) < 1e-4,
          str(tuple(item.scale)))
    item.scale = (1.0, 1.0)

    # rotate: start at right of center, drag to above center -> +90 deg
    item.rotation = 0.0
    ct._refboard_drag_set({
        "index": 0, "mode": 'rotate', "sub": 1, "temp": False,
        "moved": False, "m0": (500, 300), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (100.0, 0.0),
        "angle0": _m.atan2(0, 100), "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 400, 400)
    check("rotate 90deg", abs(item.rotation - _m.pi / 2) < 1e-4,
          str(item.rotation))

    # ctrl + rotate: snap to absolute 5-degree increments. Same drag to
    # (400,400) gives ~90deg; drag to 407,393 gives ~82.5deg -> snaps to 85.
    item.rotation = 0.0
    ct._refboard_drag_set({
        "index": 0, "mode": 'rotate', "sub": 1, "temp": False,
        "moved": False, "m0": (500, 300), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (100.0, 0.0),
        "angle0": _m.atan2(0, 100), "dist0": 1.0, "screen_dist0": 1.0})
    ev = types.SimpleNamespace(ctrl=True)
    ct._refboard_drag_update(scene, reg, 407, 393, ev)
    # Center is at (400,300); atan2(93, 7) ~= 85.7deg -> snaps to 85.
    raw = _m.atan2(393 - 300, 407 - 400)
    snapped = _m.radians(5.0) * round(raw / _m.radians(5.0))
    # rotation is a float32 FloatProperty, so allow 1e-5.
    check("ctrl snap 5deg", abs(item.rotation - snapped) < 1e-5,
          str(item.rotation))
    check("snap is quantized",
          abs(item.rotation / _m.radians(5.0)
              - round(item.rotation / _m.radians(5.0))) < 1e-4)

    # crop: drag right border left to x=430 -> r = 30/100 + 0.5 = 0.8
    item.rotation = 0.0; item.scale = (1.0, 1.0)
    item.crop = (0.0, 0.0, 1.0, 1.0)
    ct._refboard_drag_set({
        "index": 0, "mode": 'crop', "sub": 2, "temp": False,
        "moved": False, "m0": (450, 300), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "local0": (50.0, 0.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 430, 300)
    check("crop right edge", abs(item.crop[2] - 0.8) < 1e-4,
          str(tuple(item.crop)))
    check("crop keeps texel size", abs(item.scale[0] - 1.0) < 1e-6)

    # restore snapshot on cancel
    ct._refboard_restore(item, ((0.5, 0.5), (1.0, 1.0), 0.0,
                               (0.0, 0.0, 1.0, 1.0), 1.0))
    check("snapshot restore", abs(item.crop[2] - 1.0) < 1e-6)

    # Screen-space marquee crop on a ROTATED image: the crop is stored as
    # a UV quad that renders back as exactly the screen-aligned marquee
    # rect - the image keeps its rotation.
    item.pos = (0.5, 0.5)
    item.rotation = _m.radians(30.0)
    item.crop = (0.0, 0.0, 1.0, 1.0)
    ok = ct._refboard_apply_crop_rect(scene, reg, 0, 370, 290, 430, 310)
    check("rot marquee crop applied", ok)
    fq = ct._refboard_crop_quad(item, reg, item.image)
    check("crop quad is the marquee",
          abs(fq[0][0] - 370) < 0.5 and abs(fq[0][1] - 290) < 0.5 and
          abs(fq[1][0] - 430) < 0.5 and abs(fq[1][1] - 290) < 0.5 and
          abs(fq[2][0] - 430) < 0.5 and abs(fq[2][1] - 310) < 0.5 and
          abs(fq[3][0] - 370) < 0.5 and abs(fq[3][1] - 310) < 0.5, str(fq))
    check("rotation preserved",
          abs(item.rotation - _m.radians(30.0)) < 1e-5)
    # Edge drag on the rotated crop: pull the right edge in by 20px.
    fq0 = ct._refboard_crop_quad(item, reg, item.image)
    ct._refboard_drag_set({
        "index": 0, "mode": 'crop', "sub": 2, "temp": False,
        "moved": False, "m0": (430, 300), "snap": ct._refboard_snapshot(item),
        "pos0": tuple(item.pos), "scale0": (1.0, 1.0),
        "rot0": item.rotation,
        "crop0": tuple(item.crop), "local0": (0.0, 0.0),
        "crop_uv0": tuple(v for p in ct._refboard_crop_uvs(item) for v in p),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 410, 300)
    fq1 = ct._refboard_crop_quad(item, reg, item.image)
    rx0 = (fq0[1][0] + fq0[2][0]) * 0.5
    rx1 = (fq1[1][0] + fq1[2][0]) * 0.5
    check("rot edge crop pulls edge in", rx1 < rx0 - 15.0,
          f"{rx0} -> {rx1}")
    ct._refboard_drag_set(None)
    ct._refboard_restore(item, ((0.5, 0.5), (1.0, 1.0), 0.0,
                               (0.0, 0.0, 1.0, 1.0), 1.0,
                               (0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0)))
    check("restore resets quad", abs(item.crop_pts[2] - 1.0) < 1e-6)

    # Re-crop a rotated+marquee-cropped ref: a big inward edge drag must
    # stop at the first ORIGINAL-quad border the new edge would cross -
    # letting it run on flips the quad into a bowtie (the skew bug).
    item.pos = (0.5, 0.5)
    item.rotation = _m.radians(40.0)
    item.crop = (0.0, 0.0, 1.0, 1.0)
    ct._refboard_apply_crop_rect(scene, reg, 0, 370, 270, 440, 330)
    base_pts = [tuple(p) for p in ct._refboard_crop_uvs(item)]
    bcx = sum(p[0] for p in base_pts) * 0.25
    bcy = sum(p[1] for p in base_pts) * 0.25

    def _in_quad(pt):
        for j in range(4):
            ax_, ay_ = base_pts[j]
            ex_, ey_ = base_pts[(j + 1) % 4][0] - ax_, \
                base_pts[(j + 1) % 4][1] - ay_
            cs = ex_ * (bcy - ay_) - ey_ * (bcx - ax_)
            if abs(cs) < 1e-12:
                continue
            sgn = 1.0 if cs > 0.0 else -1.0
            if sgn * (ex_ * (pt[1] - ay_) - ey_ * (pt[0] - ax_)) \
                    < -1e-6:
                return False
        return True

    ct._refboard_drag_set({
        "index": 0, "mode": 'crop', "sub": 2, "temp": False,
        "moved": False, "m0": (440, 300), "snap": ct._refboard_snapshot(item),
        "pos0": tuple(item.pos), "scale0": (1.0, 1.0),
        "rot0": item.rotation,
        "crop0": tuple(item.crop), "local0": (0.0, 0.0),
        "crop_uv0": tuple(v for p in ct._refboard_crop_uvs(item) for v in p),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 330, 300)  # far past the opposite edge
    pts2 = [tuple(p) for p in ct._refboard_crop_uvs(item)]
    # moved edge endpoints: sub 2 = right edge = pts 1-2
    check("recrop stays inside original quad",
          _in_quad(pts2[1]) and _in_quad(pts2[2]),
          str(pts2))
    # convexity: consecutive edge cross products share one sign
    signs = []
    for i in range(4):
        a, b, c = pts2[i], pts2[(i + 1) % 4], pts2[(i + 2) % 4]
        signs.append((b[0] - a[0]) * (c[1] - b[1]) -
                     (b[1] - a[1]) * (c[0] - b[0]))
    check("recrop quad stays convex (no bowtie)",
          all(x > -1e-9 for x in signs) or
          all(x < 1e-9 for x in signs), str(signs))
    ct._refboard_drag_set(None)
    ct._refboard_restore(item, ((0.5, 0.5), (1.0, 1.0), 0.0,
                               (0.0, 0.0, 1.0, 1.0), 1.0,
                               (0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0)))

    # Rotated + off-center marquee crop: rotate and center-scale must
    # pivot on the CROP quad's centroid, not the uncropped image center
    # (pos) - the visible chunk must not orbit or drift.
    item.pos = (0.5, 0.5); item.scale = (1.0, 1.0)
    item.rotation = _m.radians(30.0)
    item.crop = (0.0, 0.0, 1.0, 1.0)
    ct._refboard_apply_crop_rect(scene, reg, 0, 400, 280, 440, 320)
    q = ct._refboard_crop_quad(item, reg, item.image)
    cx0 = sum(p[0] for p in q) * 0.25
    cy0 = sum(p[1] for p in q) * 0.25
    ct._refboard_start_drag(scene, reg, 0, 'rotate', 0, cx0 + 60, cy0)
    ct._refboard_drag_update(scene, reg, cx0, cy0 + 60)
    q = ct._refboard_crop_quad(item, reg, item.image)
    cx1 = sum(p[0] for p in q) * 0.25
    cy1 = sum(p[1] for p in q) * 0.25
    check("rotate pivots crop center",
          abs(cx1 - cx0) < 0.5 and abs(cy1 - cy0) < 0.5,
          f"{cx0:.1f},{cy0:.1f} -> {cx1:.1f},{cy1:.1f}")
    ct._refboard_drag_set(None)
    item.pos = (0.5, 0.5); item.scale = (1.0, 1.0)
    item.rotation = _m.radians(30.0)
    item.crop = (0.0, 0.0, 1.0, 1.0)
    ct._refboard_apply_crop_rect(scene, reg, 0, 400, 280, 440, 320)
    q = ct._refboard_crop_quad(item, reg, item.image)
    cx0 = sum(p[0] for p in q) * 0.25
    cy0 = sum(p[1] for p in q) * 0.25
    ct._refboard_start_drag(scene, reg, 0, 'center_scale', -1, 400, 300)
    ct._refboard_drag_update(scene, reg, 525, 300)
    q = ct._refboard_crop_quad(item, reg, item.image)
    cx1 = sum(p[0] for p in q) * 0.25
    cy1 = sum(p[1] for p in q) * 0.25
    check("center_scale pivots crop center",
          abs(cx1 - cx0) < 0.5 and abs(cy1 - cy0) < 0.5,
          f"{cx0:.1f},{cy0:.1f} -> {cx1:.1f},{cy1:.1f}")
    ct._refboard_drag_set(None)
    # Ctrl+double-click on a crop edge restores the full frame.
    ct._refboard_crop_commit(
        item, [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    check("dblclick restores full crop",
          abs(item.crop[2] - 1.0) < 1e-6 and
          abs(item.crop_pts[4] - 1.0) < 1e-6,
          f"crop={tuple(item.crop)}")
    ct._refboard_restore(item, ((0.5, 0.5), (1.0, 1.0), 0.0,
                               (0.0, 0.0, 1.0, 1.0), 1.0,
                               (0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0)))

    # Crop-edge drag on a SHEARED quad: rotate + screen marquee gives a
    # parallelogram in UV space, so the drag must track the mouse along the
    # edge's SCREEN normal (a UV-space slide shears the window - the bug).
    item.pos = (0.5, 0.5); item.scale = (1.0, 1.0)
    item.rotation = _m.radians(30.0)
    item.crop = (0.0, 0.0, 1.0, 1.0)
    ct._refboard_apply_crop_rect(scene, reg, 0, 370, 290, 430, 310)
    for sub, emid, target in (
            (2, (430.0, 300.0), (410.0, 300.0)),   # right edge left
            (0, (370.0, 300.0), (385.0, 300.0)),   # left edge right
            (3, (400.0, 310.0), (400.0, 302.0)),   # top edge down
            (1, (400.0, 290.0), (400.0, 298.0))):  # bottom edge up
        ct._refboard_apply_crop_rect(scene, reg, 0, 370, 290, 430, 310)
        q0 = ct._refboard_crop_quad(item, reg, item.image)
        ai, bi = {0: (3, 0), 1: (0, 1), 2: (1, 2), 3: (2, 3)}[sub]
        e0 = ((q0[ai][0] + q0[bi][0]) * 0.5, (q0[ai][1] + q0[bi][1]) * 0.5)
        ct._refboard_drag_set({
            "index": 0, "mode": 'crop', "sub": sub, "temp": False,
            "moved": False, "m0": emid,
            "snap": ct._refboard_snapshot(item),
            "pos0": tuple(item.pos), "scale0": tuple(item.scale),
            "rot0": item.rotation,
            "crop0": tuple(item.crop), "local0": (0.0, 0.0),
            "crop_uv0": tuple(v for p in ct._refboard_crop_uvs(item)
                              for v in p),
            "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
        ct._refboard_drag_update(scene, reg, target[0], target[1])
        q1 = ct._refboard_crop_quad(item, reg, item.image)
        e1 = ((q1[ai][0] + q1[bi][0]) * 0.5, (q1[ai][1] + q1[bi][1]) * 0.5)
        # The dragged edge's midpoint must land on the mouse along the
        # edge's screen normal; the OPPOSITE direction must not shift
        # (that would be the old skewed behavior).
        ex, ey = q0[bi][0] - q0[ai][0], q0[bi][1] - q0[ai][1]
        el = max(1e-9, _m.hypot(ex, ey))
        nx, ny = -ey / el, ex / el
        got = (e1[0] - e0[0]) * nx + (e1[1] - e0[1]) * ny
        want = (target[0] - e0[0]) * nx + (target[1] - e0[1]) * ny
        # signed: normal may point either way, both measurements share it
        off_axis = abs((e1[0] - e0[0]) * (ex / el) +
                       (e1[1] - e0[1]) * (ey / el))
        check(f"crop edge {sub} tracks mouse along normal",
              abs(got - want) < 0.6, f"got {got:.2f} want {want:.2f}")
        check(f"crop edge {sub} slides parallel (no skew)",
              off_axis < 0.6, f"off-axis {off_axis:.2f}")
        ct._refboard_drag_set(None)

    # Dragging a crop edge OUTWARD un-crops: the window grows back toward
    # the image bounds but stops at the texture edge (clamp), staying a
    # parallel slide rather than skewing.
    ct._refboard_apply_crop_rect(scene, reg, 0, 370, 290, 430, 310)
    ct._refboard_drag_set({
        "index": 0, "mode": 'crop', "sub": 2, "temp": False,
        "moved": False, "m0": (430.0, 300.0),
        "snap": ct._refboard_snapshot(item),
        "pos0": tuple(item.pos), "scale0": tuple(item.scale),
        "rot0": item.rotation,
        "crop0": tuple(item.crop), "local0": (0.0, 0.0),
        "crop_uv0": tuple(v for p in ct._refboard_crop_uvs(item)
                          for v in p),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    q0 = ct._refboard_crop_quad(item, reg, item.image)
    rx0 = (q0[1][0] + q0[2][0]) * 0.5
    ct._refboard_drag_update(scene, reg, 470, 300)  # 40px outward
    q1 = ct._refboard_crop_quad(item, reg, item.image)
    rx1 = (q1[1][0] + q1[2][0]) * 0.5
    check("crop edge outward un-crops", rx1 > rx0 + 1.0,
          f"{rx0:.1f} -> {rx1:.1f}")
    check("un-crop stays inside texture",
          all(-1e-4 <= u <= 1.0 + 1e-4 and -1e-4 <= v <= 1.0 + 1e-4
              for u, v in ct._refboard_crop_uvs(item)),
          str(ct._refboard_crop_uvs(item)))
    # The slide halts when the FIRST corner touches an image border -
    # letting the other endpoint run on would skew the edge.
    uvs = ct._refboard_crop_uvs(item)
    check("un-crop stops at border touch",
          any(abs(u) < 1e-3 or abs(u - 1.0) < 1e-3 or
              abs(v) < 1e-3 or abs(v - 1.0) < 1e-3
              for u, v in (uvs[1], uvs[2])),
          str(uvs))
    # Still parallel: the dragged edge direction must not rotate.
    e0 = (q0[2][0] - q0[1][0], q0[2][1] - q0[1][1])
    e1 = (q1[2][0] - q1[1][0], q1[2][1] - q1[1][1])
    cross = e0[0] * e1[1] - e0[1] * e1[0]
    check("un-crop edge stays parallel",
          abs(cross) / max(1e-9, _m.hypot(*e0) * _m.hypot(*e1)) < 1e-3)

    # Crop-edge pick on a SKEWED quad: the fitted rect's edge strips can
    # diverge from the drawn edges (top edge reads as 'inside' or the
    # opposite edge), so hovering must test the real quad segments.
    ct._refboard_crop_commit(
        item, [(0.05, 0.4), (0.95, 0.1), (0.9, 0.6), (0.3, 0.95)])
    qq = ct._refboard_crop_quad(item, reg, item.image)
    for sub, (a, b) in ((0, (3, 0)), (1, (0, 1)),
                        (2, (1, 2)), (3, (2, 3))):
        emx = (qq[a][0] + qq[b][0]) * 0.5
        emy = (qq[a][1] + qq[b][1]) * 0.5
        zz = ct._refboard_crop_edge_zone(
            item, reg, ct._refboard_view(scene, reg), emx, emy)
        check(f"skewed quad edge pick sub={sub}",
              zz == ('crop', sub), str(zz))
    item.crop = (0.0, 0.0, 1.0, 1.0)
    ct._refboard_drag_set(None)
    ct._refboard_restore(item, ((0.5, 0.5), (1.0, 1.0), 0.0,
                               (0.0, 0.0, 1.0, 1.0), 1.0,
                               (0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0),
                               (False, False)))
    check("restore clears flips",
          not item.flip_x and not item.flip_y)
    item.rotation = 0.0

    # Flips permute the sampled texcoords without moving the quad.
    u = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    check("flip_x permutes uvs",
          ct._refboard_flip_uvs(
              types.SimpleNamespace(flip_x=True, flip_y=False), u) ==
          [(1.0, 0.0), (0.0, 0.0), (0.0, 1.0), (1.0, 1.0)])
    check("flip_y permutes uvs",
          ct._refboard_flip_uvs(
              types.SimpleNamespace(flip_x=False, flip_y=True), u) ==
          [(0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)])
    check("flip both = 180 rotation of texcoords",
          ct._refboard_flip_uvs(
              types.SimpleNamespace(flip_x=True, flip_y=True), u) ==
          [(1.0, 1.0), (0.0, 1.0), (0.0, 0.0), (1.0, 0.0)])
    check("unflipped uvs unchanged",
          ct._refboard_flip_uvs(
              types.SimpleNamespace(flip_x=False, flip_y=False), u) == u)

    # Snapshot round-trip carries flip state (drag-cancel correctness).
    item.flip_x = True
    snap = ct._refboard_snapshot(item)
    item.flip_x = False
    ct._refboard_restore(item, snap)
    check("snapshot restores flip", item.flip_x)
    item.flip_x = False
    # -150px from 0.6 -> 0.1; hard floor at 5%.
    item.opacity = 1.0
    ct._refboard_drag_set({
        "index": 0, "mode": 'opacity', "sub": -1, "temp": False,
        "moved": False, "m0": (500, 300), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "op0": 1.0, "local0": (0.0, 0.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 650, 300)
    check("opacity clamps at 100", abs(item.opacity - 1.0) < 1e-6,
          str(item.opacity))
    item.opacity = 0.6
    ct._refboard_drag_set({
        "index": 0, "mode": 'opacity', "sub": -1, "temp": False,
        "moved": False, "m0": (500, 300), "snap": ct._refboard_snapshot(item),
        "pos0": (0.5, 0.5), "scale0": (1.0, 1.0), "rot0": 0.0,
        "crop0": (0.0, 0.0, 1.0, 1.0), "op0": 0.6, "local0": (0.0, 0.0),
        "angle0": 0.0, "dist0": 1.0, "screen_dist0": 1.0})
    ct._refboard_drag_update(scene, reg, 350, 300)
    check("opacity -150px", abs(item.opacity - 0.1) < 1e-5,
          str(item.opacity))
    ct._refboard_drag_update(scene, reg, 100, 300)
    check("opacity floor 1pct", abs(item.opacity - 0.01) < 1e-5,
          str(item.opacity))
    item.opacity = 1.0

    # visibility: hidden items aren't pickable and hiding deselects
    ct._refboard_drag_set(None)
    item.pos = (0.5, 0.5); item.scale = (1.0, 1.0)
    item.rotation = 0.0; item.crop = (0.0, 0.0, 1.0, 1.0)
    scene.refboard_selected = 0
    item.visible = False
    check("hide deselects", scene.refboard_selected == -1)
    check("hidden not pickable",
          ct._refboard_pick(scene, reg, 400, 300) is None)
    item.visible = True
    scene.refboard_selected = 0
    check("shown pickable",
          ct._refboard_pick(scene, reg, 400, 300) is not None)

    # locked: still drawn but not pickable; locking deselects
    item.locked = True
    check("lock deselects", scene.refboard_selected == -1)
    check("locked not pickable",
          ct._refboard_pick(scene, reg, 400, 300) is None)
    item.locked = False
    scene.refboard_selected = 0
    check("unlocked pickable",
          ct._refboard_pick(scene, reg, 400, 300) is not None)

    # global eye: master switch without touching per-item flags
    bpy.ops.refboard.toggle_all()
    check("global hide kills pick",
          ct._refboard_pick(scene, reg, 400, 300) is None)
    check("global hide deselects", scene.refboard_selected == -1)
    bpy.ops.refboard.toggle_all()
    scene.refboard_selected = 0
    check("global show restores pick",
          ct._refboard_pick(scene, reg, 400, 300) is not None)
    check("per-item visible kept", item.visible)

    # ------------------------------------------------------------------
    section("P4c marquee group select and collective edits")
    # Two items side by side in an 800x600 region.
    item.pos = (0.3, 0.5)
    item.scale = (1.0, 1.0)
    item.rotation = 0.0
    item.crop = (0.0, 0.0, 1.0, 1.0)
    item.opacity = 1.0
    it_b = scene.refboard_items.add()
    it_b.image = big
    it_b.pos = (0.7, 0.5)
    it_b.scale = (1.0, 1.0)
    ct._refboard_group = [0, 1]
    scene.refboard_selected = -1
    grect = ct._refboard_group_rect(scene, reg)
    # item0 x:240+/-50 -> 190-290; item1 x:560+/-50 -> 510-610; y 275-325
    check("group bbox", grect is not None and
          abs(grect[2] - 210) < 1e-4 and abs(grect[3] - 25) < 1e-4 and
          abs(grect[4] - 400) < 1e-4 and abs(grect[5] - 300) < 1e-4,
          str(grect))
    check("marquee catches both",
          ct._refboard_marquee_members(scene, reg, 0, 0, 800, 600) == [0, 1])
    check("marquee partial",
          ct._refboard_marquee_members(scene, reg, 0, 0, 400, 600) == [0])
    hit = ct._refboard_pick(scene, reg, 400, 300)
    check("pick group inside", hit == ('group', 'inside', -1), str(hit))
    hit = ct._refboard_pick(scene, reg, 610, 275)
    check("pick group corner", hit == ('group', 'corner', 1), str(hit))
    # move: every member translates by the same canvas delta
    ct._refboard_group_start_drag(scene, reg, 'inside', -1, 400, 300)
    ct._refboard_drag_update(scene, reg, 450, 320, None)
    check("group move", abs(item.pos[0] - (0.3 + 50 / 800)) < 1e-5 and
          abs(it_b.pos[1] - (0.5 + 20 / 600)) < 1e-5,
          str((tuple(item.pos), tuple(it_b.pos))))
    ct._refboard_drag = None
    # corner scale: pivot is the opposite bbox corner (bl 190,275 for
    # grabbed tr corner index 2 at 610,325)
    item.pos = (0.3, 0.5)
    item.scale = (1.0, 1.0)
    it_b.pos = (0.7, 0.5)
    it_b.scale = (1.0, 1.0)
    ct._refboard_group_start_drag(scene, reg, 'corner', 2, 610, 325)
    ct._refboard_drag_update(scene, reg, 800, 325, None)
    f = ((610 * 420) + (50 * 50)) / float(420 * 420 + 50 * 50)
    exp = (190 + (240 - 190) * f) / 800
    check("group corner scale pivot",
          abs(item.pos[0] - exp) < 1e-4 and
          abs(item.scale[0] - f) < 1e-4,
          str((tuple(item.pos), tuple(item.scale))))
    ct._refboard_drag = None
    # rotate: 90 deg about the bbox center repositions and re-angles all
    item.pos = (0.3, 0.5)
    item.rotation = 0.0
    it_b.pos = (0.7, 0.5)
    it_b.rotation = 0.0
    ct._refboard_group_start_drag(scene, reg, 'rotate', 0, 500, 300)
    ct._refboard_drag_update(scene, reg, 400, 400, None)
    check("group rotate pos", abs(item.pos[0] - 0.5) < 1e-4 and
          abs(item.pos[1] - 140 / 600) < 1e-4,
          str(tuple(item.pos)))
    check("group rotate angle",
          abs(item.rotation - _m.pi / 2) < 1e-4,
          str(item.rotation))
    ct._refboard_drag = None
    # opacity: shared delta clamped per member
    item.opacity = 0.5
    it_b.opacity = 1.0
    ct._refboard_group_start_drag(scene, reg, 'opacity', -1, 500, 300,
                                 'RIGHTMOUSE')
    ct._refboard_drag_update(scene, reg, 350, 300, None)
    check("group opacity", abs(item.opacity - 0.01) < 1e-5 and
          abs(it_b.opacity - 0.5) < 1e-5,
          str((item.opacity, it_b.opacity)))
    ct._refboard_drag = None
    # Arrange Auto: packs the pair inside the group bbox, no scaling, so
    # both end up adjacent horizontally, roughly centered.
    item.pos = (0.3, 0.5)
    item.scale = (1.0, 1.0)
    item.rotation = 0.0
    it_b.pos = (0.7, 0.5)
    it_b.scale = (1.0, 1.0)
    it_b.rotation = 0.0
    check("arrange packs pair", ct._refboard_arrange_auto(scene, reg))
    # Two 100px-wide items + 6px pad in a 420px-wide bbox, centered:
    # centers at 400-53=347px and 400+53=453px -> pos 0.43375 / 0.56625.
    check("arrange pos", abs(item.pos[0] - 0.43375) < 1e-3 and
          abs(it_b.pos[0] - 0.56625) < 1e-3,
          str((tuple(item.pos), tuple(it_b.pos))))
    check("arrange keeps scale", tuple(item.scale) == (1.0, 1.0) and
          tuple(it_b.scale) == (1.0, 1.0))
    ct._refboard_group = []
    scene.refboard_items.remove(1)
    scene.refboard_selected = 0
    item.pos = (0.5, 0.5)
    item.rotation = 0.0
    item.opacity = 1.0

    # ------------------------------------------------------------------
    section("P5 persistence through save/reload")
    save_path = os.path.join(tmp, "persist.blend")
    # Start from a packed image on purpose: saving must externalize it into the
    # sibling repo, which is also how old packed scenes get migrated.
    packed_img = bpy.data.images.load(
        make_png(os.path.join(tmp, "persist.png")), check_existing=False)
    packed_img.pack()
    item.image = packed_img
    item.pos = (0.33, 0.66)
    item.scale = (0.5, 0.75)
    item.rotation = 0.25
    bpy.ops.wm.save_as_mainfile(filepath=save_path)
    bpy.ops.wm.open_mainfile(filepath=save_path)
    scene2 = bpy.context.scene
    check("item survived reload", len(scene2.refboard_items) == 1)
    if len(scene2.refboard_items) == 1:
        it2 = scene2.refboard_items[0]
        check("pos survived", abs(it2.pos[0] - 0.33) < 1e-6 and
              abs(it2.pos[1] - 0.66) < 1e-6, str(tuple(it2.pos)))
        check("scale survived", abs(it2.scale[1] - 0.75) < 1e-6)
        check("rotation survived", abs(it2.rotation - 0.25) < 1e-6)
        check("image survived", it2.image is not None)
        check("image externalized on save",
              it2.image is not None and it2.image.packed_file is None,
              "still packed" if it2.image else "no image")
        check("image path is repo-relative",
              it2.image is not None and
              it2.image.filepath_raw.startswith("//refboard"),
              it2.image.filepath_raw if it2.image else "")
        check("repo file exists on disk",
              it2.image is not None and
              os.path.isfile(bpy.path.abspath(it2.image.filepath_raw)))
        check("repo folder is beside the blend",
              os.path.isdir(os.path.join(os.path.dirname(save_path),
                                         "refboard")))

    # Incremental saves share one repo: saving a v002 beside v001 must reuse the
    # same file rather than copying the image again.
    repo_dir = os.path.join(os.path.dirname(save_path), "refboard")
    before = set(os.listdir(repo_dir)) if os.path.isdir(repo_dir) else set()
    v2 = os.path.join(tmp, "persist_v002.blend")
    bpy.ops.wm.save_as_mainfile(filepath=v2)
    after = set(os.listdir(repo_dir)) if os.path.isdir(repo_dir) else set()
    check("incremental save adds no duplicate files", before == after,
          "%d -> %d" % (len(before), len(after)))
    scene = bpy.context.scene

    # ------------------------------------------------------------------
    section("P6 delete and clear")
    scene.refboard_selected = 0
    doomed = scene.refboard_items[0].image if scene.refboard_items else None
    doomed_name = doomed.name if doomed else ""
    check("delete selected", ct._refboard_delete_selected(scene))
    check("items empty after delete", len(scene.refboard_items) == 0)
    check("selection cleared", scene.refboard_selected == -1)
    check("delete unloads the image datablock",
          doomed_name and doomed_name not in bpy.data.images,
          doomed_name)
    check("delete with nothing", not ct._refboard_delete_selected(scene))
    # clear op
    png3 = make_png(os.path.join(tmp, "p3.png"))
    ct._refboard_finish({"dst": png3, "mode": 'SCREEN',
                        "state": {"pos": (0.5, 0.5), "scene": scene,
                                  "rw": 800, "rh": 600}})
    check("re-added for clear", len(scene.refboard_items) == 1)
    cleared_name = scene.refboard_items[0].image.name
    n_images = len(bpy.data.images)
    bpy.ops.refboard.clear()
    check("clear all", len(scene.refboard_items) == 0)
    check("clear unloads image datablocks",
          cleared_name not in bpy.data.images,
          "%d images before, %d after" % (n_images, len(bpy.data.images)))

    # An image still used elsewhere must survive a board delete.
    png4 = make_png(os.path.join(tmp, "p4keep.png"))
    ct._refboard_finish({"dst": png4, "mode": 'SCREEN',
                        "state": {"pos": (0.5, 0.5), "scene": scene,
                                  "rw": 800, "rh": 600}})
    shared = scene.refboard_items[0].image
    shared_name = shared.name
    keeper = bpy.data.objects.new("KeepMe", None)
    keeper.empty_display_type = 'IMAGE'
    keeper.data = shared
    scene.collection.objects.link(keeper)
    scene.refboard_selected = 0
    ct._refboard_delete_selected(scene)
    check("shared image is kept while still in use",
          shared_name in bpy.data.images, shared_name)
    bpy.data.objects.remove(keeper)
    ct._refboard_gc_images()
    check("shared image unloads once unused",
          shared_name not in bpy.data.images, shared_name)

    # ------------------------------------------------------------------
    section("P6b scale pivots match the drawn handles")
    # Regression: rotate an image, then crop it with a screen marquee. UV space
    # is normalized, so a UV-space rotation shears once the non-square image
    # dimensions are applied, and the stored crop quad is a parallelogram in
    # pixel space. The handles are drawn on the best-fit rect, so scaling had
    # to pivot on that same rect or the anchor drifted.
    def pivot_drift(rot, crop_pts, sub, zone):
        scene.refboard_items.clear()
        it = scene.refboard_items.add()
        it.image = bpy.data.images.load(make_png(
            os.path.join(tmp, "pivot_%s.png" % uuid.uuid4().hex), 64, 48))
        it.pos = (0.5, 0.5)
        it.scale = (2.0, 2.0)
        it.rotation = rot
        ct._refboard_crop_commit(it, crop_pts)
        reg = fake_region(800, 600)

        def quad():
            return ct._refboard_quad(ct._refboard_rect(it, reg, it.image))

        def pt(q, i):
            if zone == 'corner':
                return q[i % 4]
            a, b = q[i % 4], q[(i + 1) % 4]
            return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)

        q0 = quad()
        anchor0 = pt(q0, sub + 2)
        gx, gy = pt(q0, sub)
        ct._refboard_start_drag(scene, reg, 0, zone, sub, gx, gy)
        vx, vy = gx - anchor0[0], gy - anchor0[1]
        ct._refboard_drag_update(scene, reg, gx + vx * 0.5, gy + vy * 0.5,
                                 None)
        ct._refboard_drag_set(None)
        anchor1 = pt(quad(), sub + 2)
        return _m.hypot(anchor1[0] - anchor0[0], anchor1[1] - anchor0[1])

    ca, sa = _m.cos(_m.radians(-30)), _m.sin(_m.radians(-30))

    def rotuv(u, v, cu=0.4, cv=0.45):
        du, dv = u - cu, v - cv
        return (cu + du * ca - dv * sa, cv + du * sa + dv * ca)

    cases = (
        ("square crop", 0.0, [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]),
        ("offset crop", 0.0, [(0.1, 0.2), (0.6, 0.2), (0.6, 0.7), (0.1, 0.7)]),
        ("rotated, offset crop", _m.radians(30),
         [(0.1, 0.2), (0.6, 0.2), (0.6, 0.7), (0.1, 0.7)]),
        ("rotated + marquee crop", _m.radians(30),
         [rotuv(*p) for p in [(0.15, 0.25), (0.65, 0.25),
                              (0.65, 0.65), (0.15, 0.65)]]),
    )
    for label, rot, pts_uv in cases:
        for zone in ('corner', 'edge'):
            worst = max(pivot_drift(rot, pts_uv, s, zone) for s in range(4))
            check("%s: %s anchor stays put" % (label, zone), worst < 1.0,
                  "%.2f px" % worst)
    scene.refboard_items.clear()
    scene.refboard_selected = -1

    # ------------------------------------------------------------------
    section("P6c modal ignores clicks over Blender's own UI regions")
    # Region overlap means the 3D view's WINDOW region extends underneath the
    # sidebar, so coordinates alone cannot tell viewport from N-panel.
    fake_area = types.SimpleNamespace(
        type='VIEW_3D',
        regions=[types.SimpleNamespace(type='WINDOW', x=0, y=0,
                                       width=800, height=600),
                 types.SimpleNamespace(type='UI', x=620, y=40,
                                       width=180, height=520),
                 types.SimpleNamespace(type='HEADER', x=0, y=574,
                                       width=800, height=26),
                 types.SimpleNamespace(type='TOOLS', x=0, y=40,
                                       width=1, height=520)])
    over_panel = types.SimpleNamespace(mouse_x=700, mouse_y=300)
    over_header = types.SimpleNamespace(mouse_x=400, mouse_y=580)
    over_view = types.SimpleNamespace(mouse_x=300, mouse_y=300)
    collapsed = types.SimpleNamespace(mouse_x=0, mouse_y=300)
    check("sidebar is treated as UI",
          ct._refboard_over_ui(fake_area, over_panel))
    check("header is treated as UI",
          ct._refboard_over_ui(fake_area, over_header))
    check("open viewport is not UI",
          not ct._refboard_over_ui(fake_area, over_view))
    check("collapsed region is ignored",
          not ct._refboard_over_ui(fake_area, collapsed))

    # ------------------------------------------------------------------
    section("P6d addon glyphs never dirty bpy.data")
    # Regression: the icon loader used to rewrite the pixels of a loaded PNG
    # and keep the datablock, so Blender asked "Save modified images?" on quit
    # and wrote the addon's own UI glyphs into the user's file.
    ct._refboard_icon_cache.clear()
    img_dir = os.path.join(os.path.dirname(ct.__file__), "img")
    for _lbl, fn in ct._REFBOARD_MODE_ICONS:
        check("mode glyph %s ships with the addon" % fn,
              os.path.isfile(os.path.join(img_dir, fn)))
    names_before = {i.name for i in bpy.data.images}
    for _lbl, fn in ct._REFBOARD_MODE_ICONS:
        ct._refboard_icon(fn, target_w=ct._REFBOARD_MODE_ICON_SIZE)
    leaked = sorted({i.name for i in bpy.data.images} - names_before)
    check("icon load leaks no image datablocks", not leaked, str(leaked))
    dirty = sorted(i.name for i in bpy.data.images if i.is_dirty)
    check("no image is left modified", not dirty, str(dirty))
    ct._refboard_icon_cache.clear()
    check("edit mode flashes text, not a glyph",
          next((f for l, f in ct._REFBOARD_MODE_ICONS
                if l == "Refboard Edit"), None) is None)

    # ------------------------------------------------------------------
    section("P6e glyph downscale is area-averaged")
    # GPUTexture gives Python no sampler state, so the shader point-samples.
    # The glyph is box-filtered to its drawn size instead, which is what makes
    # the edges read smooth rather than stair-stepped.
    # 4x4 source: left half white/opaque, right half black/transparent.
    w0 = h0 = 4
    src = []
    for _y in range(h0):
        for x in range(w0):
            v = 1.0 if x < 2 else 0.0
            src.extend([v, v, v, v])
    out = ct._refboard_box_resample(src, w0, h0, 2, 2)
    check("resample returns the requested size", len(out) == 2 * 2 * 4,
          str(len(out)))
    check("flat regions are preserved",
          abs(out[0] - 1.0) < 1e-6 and abs(out[4] - 0.0) < 1e-6,
          "left=%.3f right=%.3f" % (out[0], out[4]))

    # A hard edge landing mid-cell must average to a partial value - that
    # intermediate pixel is the antialiasing.
    edge = ct._refboard_box_resample(src, w0, h0, 1, 1)
    check("edge averages to coverage", abs(edge[0] - 0.5) < 1e-6,
          "%.3f" % edge[0])
    check("alpha averages with the color", abs(edge[3] - 0.5) < 1e-6,
          "%.3f" % edge[3])

    # Odd ratios must still cover every source pixel exactly once.
    three = ct._refboard_box_resample(src, w0, h0, 3, 3)
    check("non-integer ratio produces no empty cells",
          len(three) == 3 * 3 * 4 and all(v >= 0.0 for v in three))

    # Upscaling is left to the GPU: the loader only filters when shrinking.
    ct._refboard_icon_cache.clear()
    huge = ct._refboard_icon(ct._REFBOARD_MODE_ICONS[0][1], target_w=100000)
    check("absurd target does not resample", huge is None or huge[1] != 100000,
          str(huge[1] if huge else None))
    ct._refboard_icon_cache.clear()

    # ------------------------------------------------------------------
    section("P6f mode flash is suppressed on an empty board")
    saved_label = ct._refboard_mode_label
    scene.refboard_items.clear()
    ct._refboard_mode_label = "Refboard Off"
    check("empty board flashes nothing",
          not ct._refboard_mode_flash_visible(scene))
    ct._refboard_mode_label = "Refboard Edit"
    check("empty board still flashes edit mode",
          ct._refboard_mode_flash_visible(scene))

    png_flash = make_png(os.path.join(tmp, "flash.png"))
    ct._refboard_finish({"dst": png_flash, "mode": 'SCREEN',
                        "state": {"pos": (0.5, 0.5), "scene": scene,
                                  "rw": 800, "rh": 600}})
    check("board with a ref flashes",
          ct._refboard_mode_flash_visible(scene))
    # Hiding blanks the caller's item list, but "hidden" is itself a mode that
    # should still flash.
    scene.refboard_all_hidden = True
    check("hidden board still flashes",
          ct._refboard_mode_flash_visible(scene))
    scene.refboard_all_hidden = False
    ct._refboard_mode_label = ""
    check("no label flashes nothing",
          not ct._refboard_mode_flash_visible(scene))
    ct._refboard_mode_label = saved_label

    # The draw gate: an empty board draws nothing... unless edit mode is
    # on, where the veil must show even before the first paste.
    saved_canvas = ct._refboard_canvas_on
    saved_pending = ct._refboard_pending
    try:
        ct._refboard_mode_label = ""
        ct._refboard_pending = []
        ct._refboard_canvas_on = False
        check("empty board wants no draw",
              not ct._refboard_draw_wanted(scene, []))
        ct._refboard_canvas_on = True
        check("edit mode wants the veil when empty",
              ct._refboard_draw_wanted(scene, []))
    finally:
        ct._refboard_canvas_on = saved_canvas
        ct._refboard_pending = saved_pending
    bpy.ops.refboard.clear()

    # ------------------------------------------------------------------
    section("P6g in-process clipboard read")
    # A real 8x8 32bpp DIB: BITMAPINFOHEADER + bottom-up BGRA pixels.
    import struct as _st
    w, h = 8, 8
    dib = _st.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, w * h * 4,
                   0, 0, 0, 0) + b"\x40\x80\xC0\xFF" * (w * h)
    bmp_bytes = ct._refboard_dib_to_bmp(dib)
    check("dib wraps into a bmp", bmp_bytes is not None and
          bmp_bytes[:2] == b"BM")
    check("bmp file size in header",
          _st.unpack_from("<I", bmp_bytes, 2)[0] == 14 + len(dib))
    bmp_path = os.path.join(tmp, "dib_rt.bmp")
    with open(bmp_path, "wb") as f:
        f.write(bmp_bytes)
    bmp_img = bpy.data.images.load(bmp_path)
    check("blender loads the wrapped bmp",
          tuple(bmp_img.size) == (8, 8), str(tuple(bmp_img.size)))
    bpy.data.images.remove(bmp_img)

    # ------------------------------------------------------------------
    section("P7 clipboard image gate")
    if ct.platform.system() == "Windows":
        try:
            import subprocess as _sp
            flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
            _sp.run(["powershell", "-STA", "-NoProfile",
                     "-NonInteractive", "-Command",
                     "Set-Clipboard -Value 'refboard gate test'"],
                    capture_output=True, timeout=15, creationflags=flags)
            check("gate closed on text clipboard",
                  not ct._refboard_clipboard_has_image())
            src = make_png(os.path.join(tmp, "gate.png"))
            _sp.run(
                ["powershell", "-STA", "-NoProfile",
                 "-NonInteractive", "-Command",
                 "Add-Type -AssemblyName System.Windows.Forms;"
                 "Add-Type -AssemblyName System.Drawing;"
                 "[Windows.Forms.Clipboard]::SetImage("
                 "[System.Drawing.Bitmap]::FromFile('%s'))" % src],
                capture_output=True, timeout=15, creationflags=flags)
            check("gate open on image clipboard",
                  ct._refboard_clipboard_has_image())
        except Exception as e:
            print("  [SKIP] clipboard gate: %s" % e, flush=True)
    else:
        check("non-windows gate permissive",
              ct._refboard_clipboard_has_image())

    # ------------------------------------------------------------------
    section("P8 real clipboard end-to-end (optional)")
    try:
        import subprocess as _sp
        src = make_png(os.path.join(tmp, "clip.png"))
        flags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        setr = _sp.run(
            ["powershell", "-STA", "-NoProfile", "-NonInteractive",
             "-Command",
             "Add-Type -AssemblyName System.Windows.Forms;"
             "Add-Type -AssemblyName System.Drawing;"
             "[Windows.Forms.Clipboard]::SetImage("
             "[System.Drawing.Bitmap]::FromFile('%s'))" % src],
            capture_output=True, timeout=15, creationflags=flags)
        if setr.returncode != 0:
            raise RuntimeError("clipboard set failed: %s" % setr.stderr)
        got = ct._refboard_clipboard_image_win(
            os.path.join(tmp, "clip_out.png"))
        if not got:
            got = os.path.join(tmp, "clip_out.png")
            proc = ct._refboard_paste_proc(got)
            proc.wait(timeout=15)
        check("clipboard e2e read landed a file",
              bool(got) and os.path.isfile(got), str(got))
        ct._refboard_finish({"dst": got, "mode": 'SCREEN',
                            "state": {"pos": (0.5, 0.5), "scene": scene,
                                      "rw": 800, "rh": 600}})
        check("clipboard e2e item", len(scene.refboard_items) == 1)
        if len(scene.refboard_items) == 1:
            check("e2e image has size",
                  scene.refboard_items[0].image.size[0] == 8)
    except Exception as e:
        print("  [SKIP] clipboard e2e unavailable: %s" % e, flush=True)

    # ------------------------------------------------------------------
    section("P9 preferences + N-panel hide/restore")
    prefs = ctx.preferences.addons["refboard"].preferences
    check("bl_info version floor (bump before each release tag)",
          ct.bl_info["version"] >= (0, 2, 0), str(ct.bl_info["version"]))
    check("pref show_n_panel default off", not prefs.show_n_panel)
    check("pref show_help default on", prefs.show_help)
    check("pref help_size default", abs(prefs.help_size - 16.0) < 1e-6)
    check("help header advertises H toggle",
          ct._REFBOARD_HELP_HEADER == "Refboard Help (H)")
    check("help lines stay compact",
          all(len(v) <= 34 for _, v in ct._REFBOARD_HELP_LINES),
          str([v for _, v in ct._REFBOARD_HELP_LINES if len(v) > 34]))
    check("pref veil_alpha default",
          abs(prefs.veil_alpha - 0.7) < 1e-6)
    check("pref veil_color default",
          all(abs(a - b) < 1e-3 for a, b in
              zip(prefs.veil_color, (0.286, 0.282, 0.353))))
    check("pref read helper", ct._refboard_pref("show_help", False) is True)
    check("pref read fallback",
          ct._refboard_pref("no_such_prop", 42) == 42)
    check("scene props migrated",
          not hasattr(scene, "refboard_show_help") and
          not hasattr(scene, "refboard_veil_color"))
    check("panel poll respects pref",
          ct.REFBOARD_PT_board.poll(ctx) is False)

    class FakeSpace:
        def __init__(self, ui):
            self.show_region_ui = ui
    s_open, s_closed = FakeSpace(True), FakeSpace(False)
    ct._refboard_npanel_hide([s_open, s_closed])
    check("open panel hidden on entry", not s_open.show_region_ui)
    check("closed panel left alone", not s_closed.show_region_ui)
    ct._refboard_npanel_restore()
    check("open panel restored on exit", s_open.show_region_ui)
    check("closed panel stays closed", not s_closed.show_region_ui)
    check("hidden list cleared", len(ct._refboard_npanel_hidden) == 0)

    prefs.show_n_panel = True
    s_open2 = FakeSpace(True)
    ct._refboard_npanel_hide([s_open2])
    check("pref on: panel not hidden", s_open2.show_region_ui)
    check("panel poll passes when pref on",
          ct.REFBOARD_PT_board.poll(ctx) is True)
    prefs.show_n_panel = False

    # switch_mode drives the same helpers
    ct._refboard_canvas_on = False
    scene.refboard_all_hidden = True
    s_open3 = FakeSpace(True)
    # inject spaces via monkeypatched generator
    orig_gen = ct._refboard_view3d_spaces
    ct._refboard_view3d_spaces = lambda: iter([s_open3])
    try:
        ct._refboard_switch_mode(scene, True)   # alt: enter edit
        check("edit entry hides panel", not s_open3.show_region_ui)
        ct._refboard_switch_mode(scene, False)  # leave edit
        check("edit exit restores panel", s_open3.show_region_ui)
    finally:
        ct._refboard_view3d_spaces = orig_gen
        ct._refboard_npanel_hidden.clear()

    # ------------------------------------------------------------------
    section("P10 depth order + reset + progress")
    n0 = len(scene.refboard_items)
    mk_png = make_png(os.path.join(tmp, "p10.png"))
    for _ in range(3):
        it = scene.refboard_items.add()
        it.image = bpy.data.images.load(mk_png)
    ids = [i for i in range(n0, n0 + 3)]
    # ] : bring to front -> selected ends at the last index
    new = ct._refboard_move_depth(scene, [ids[0]], True)
    check("front: moves to last index",
          new == [len(scene.refboard_items) - 1], str(new))
    check("front: item intact",
          scene.refboard_items[new[0]].image is not None)
    # [ : send to back -> index 0
    new = ct._refboard_move_depth(scene, [new[0]], False)
    check("back: moves to index 0", new == [0], str(new))
    # group restack preserves relative order
    mem_a, mem_b = scene.refboard_items[ids[1]], scene.refboard_items[ids[2]]
    new = ct._refboard_move_depth(scene, [ids[1], ids[2]], True)
    check("group front occupies top slots",
          sorted(new) == [len(scene.refboard_items) - 2,
                          len(scene.refboard_items) - 1], str(new))
    check("group front keeps order",
          scene.refboard_items[new[0]] == mem_a and
          scene.refboard_items[new[1]] == mem_b)
    for i in range(3):
        scene.refboard_items.remove(len(scene.refboard_items) - 1)

    # Reset Image: rotation/flips/opacity back to neutral and the full
    # frame restored; size and position are kept.
    rit = scene.refboard_items.add()
    rit.image = bpy.data.images.load(mk_png)
    rit.pos = (0.33, 0.66)
    rit.scale = (2.0, 1.5)
    rit.rotation = 0.9
    rit.flip_x = True
    rit.opacity = 0.4
    ct._refboard_crop_commit(rit, [(0.2, 0.2), (0.8, 0.2),
                                   (0.8, 0.8), (0.2, 0.8)])
    ridx = len(scene.refboard_items) - 1
    reg_r = fake_region(800, 600)
    ok = ct._refboard_reset_item(scene, ridx)
    check("reset returns True", ok)
    check("reset keeps position",
          abs(rit.pos[0] - 0.33) < 1e-6 and abs(rit.pos[1] - 0.66) < 1e-6)
    check("reset keeps size",
          abs(rit.scale[0] - 2.0) < 1e-6 and abs(rit.scale[1] - 1.5) < 1e-6)
    check("reset clears rotation", abs(rit.rotation) < 1e-6)
    check("reset clears flip", not rit.flip_x and not rit.flip_y)
    check("reset restores opacity", abs(rit.opacity - 1.0) < 1e-6)
    check("reset restores crop",
          tuple(rit.crop) == (0.0, 0.0, 1.0, 1.0), str(tuple(rit.crop)))
    check("reset bad index is False",
          not ct._refboard_reset_item(scene, 9999))

    # Reset Cropping: full frame back, transform/opacity untouched.
    ok = ct._refboard_reset_crop(scene, ridx)
    check("reset crop returns True", ok)
    check("reset crop restores full frame",
          tuple(rit.crop) == (0.0, 0.0, 1.0, 1.0), str(tuple(rit.crop)))
    check("reset crop keeps pos/size",
          abs(rit.pos[0] - 0.33) < 1e-6 and abs(rit.scale[0] - 2.0) < 1e-6)
    check("reset crop bad index is False",
          not ct._refboard_reset_crop(scene, 9999))
    scene.refboard_items.remove(ridx)

    # Progress: done entries hold until the bar sweeps to ~100%, deadline
    # bounds the hold so headless contexts still finish.
    fin = []
    orig_fin = ct._refboard_finish
    ct._refboard_finish = lambda p: fin.append(p)
    fake_proc = types.SimpleNamespace(
        poll=lambda: 0, communicate=lambda timeout=1: (b"", b""))
    ct._refboard_pending.append({
        "proc": fake_proc, "dst": "/nonexistent", "mode": 'SCREEN',
        "state": {}, "t0": time.time(), "shown": 0.0})
    ct._refboard_poll_timer()
    check("done entry held while bar sweeps",
          len(ct._refboard_pending) == 1 and len(fin) == 0)
    ct._refboard_pending[0]["shown"] = 1.0
    ct._refboard_poll_timer()
    check("full bar releases the entry",
          len(ct._refboard_pending) == 0 and len(fin) == 1)
    ct._refboard_pending.append({
        "proc": fake_proc, "dst": "/nonexistent", "mode": 'SCREEN',
        "state": {}, "t0": time.time(), "shown": 0.0,
        "done": True, "done_t": time.time() - 1.0})
    ct._refboard_poll_timer()
    check("deadline finishes without redraws",
          len(ct._refboard_pending) == 0 and len(fin) == 2)
    ct._refboard_finish = orig_fin
    check("pending empty -> timer unregisters",
          ct._refboard_poll_timer() is None)

    # ------------------------------------------------------------------
    section("P11 self-update helpers")
    check("parse v0.2.0",
          ct._refboard_parse_ver("v0.2.0") == (0, 2, 0))
    check("parse bare version",
          ct._refboard_parse_ver("1.4") == (1, 4, 0))
    check("parse junk is None",
          ct._refboard_parse_ver("v.01") is None and
          ct._refboard_parse_ver("latest") is None and
          ct._refboard_parse_ver("") is None)
    tags = [{"name": "v0.1.0"}, {"name": "v.01"},
            {"name": "v0.10.0"}, {"name": "v0.9.9"}]
    best = ct._refboard_latest_tag(tags)
    check("latest tag is semver max",
          best is not None and best[0] == (0, 10, 0),
          str(best[0] if best else None))
    check("latest tag empty payload",
          ct._refboard_latest_tag([]) is None and
          ct._refboard_latest_tag([{"name": "junk"}]) is None)

    # install: zip with a GitHub-style nested root overlays cleanly and
    # leaves a backup zip in _backups/
    inst_dir = os.path.join(tmp, "inst_addon")
    os.makedirs(inst_dir, exist_ok=True)
    with open(os.path.join(inst_dir, "__init__.py"), "w") as f:
        f.write("OLD_MARKER")
    zp = os.path.join(tmp, "upd.zip")
    import zipfile as _zf
    with _zf.ZipFile(zp, "w") as z:
        z.writestr("arieldiazj-refboard-deadbeef/__init__.py", "NEW_MARKER")
        z.writestr("arieldiazj-refboard-deadbeef/tests/t.py", "TEST_FILE")
        z.writestr("arieldiazj-refboard-deadbeef/.gitignore", "*.tmp")
    ct._refboard_install_zip(zp, inst_dir)
    check("install overlays __init__.py",
          open(os.path.join(inst_dir, "__init__.py")).read() == "NEW_MARKER")
    check("install extracts nested paths",
          os.path.isfile(os.path.join(inst_dir, "tests", "t.py")))
    bks = os.listdir(os.path.join(inst_dir, "_backups"))
    check("install writes a backup zip",
          len(bks) == 1 and bks[0].startswith("refboard_v"), str(bks))

    # ------------------------------------------------------------------
    print(f"\n=== RESULT: {PASS_COUNT} passed, {FAIL_COUNT} failed ===",
          flush=True)
    return 1 if FAIL_COUNT else 0


sys.exit(main())
