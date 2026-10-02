"""Debug: rotated crop -> re-crop edge selection."""
import math as _m
import os
import sys
import tempfile
import types

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))


def fake_region(w=800, h=600):
    return types.SimpleNamespace(width=w, height=h, as_pointer=lambda: 1)


def make_png(path, w=100, h=100):
    img = bpy.data.images.new("t", width=w, height=h)
    img.pixels[:] = [0.9, 0.2, 0.2, 1.0] * (w * h)
    img.filepath_raw = path
    img.file_format = 'PNG'
    try:
        img.save()
    except Exception:
        img.save_render(path)
    bpy.data.images.remove(img)
    return path


def main():
    bpy.ops.preferences.addon_enable(module="refboard")
    import refboard as ct
    scene = bpy.context.scene
    reg = fake_region(800, 600)
    tmp = tempfile.mkdtemp(prefix="rb_dbg_")
    png = make_png(os.path.join(tmp, "a.png"))

    for rot_deg in (0.0, 30.0, 120.0, 200.0):
        scene.refboard_items.clear()
        it = scene.refboard_items.add()
        it.image = bpy.data.images.load(png)
        it.pos = (0.5, 0.5)
        it.scale = (1.0, 1.0)
        it.rotation = _m.radians(rot_deg)
        it.crop = (0.0, 0.0, 1.0, 1.0)
        scene.refboard_selected = 0
        # screen marquee crop 370..430 x 290..310
        ct._refboard_apply_crop_rect(scene, reg, 0, 370, 290, 430, 310)
        print(f"\n=== rot {rot_deg} ===")
        print("crop_pts:", [tuple(round(v, 3) for v in
                             it.crop_pts[i:i + 2])
                            for i in range(0, 8, 2)])
        q = ct._refboard_crop_quad(it, reg, it.image)
        print("quad:", [(round(x, 1), round(y, 1)) for x, y in q])
        rect = ct._refboard_rect(it, reg, it.image,
                                 ct._refboard_view(scene, reg))
        print("rect:", [round(v, 2) for v in rect])
        # where does the rect's top strip sit vs the quad edges?
        top_mid = ct._refboard_to_screen(rect, rect[0], rect[1] + rect[3])
        bot_mid = ct._refboard_to_screen(rect, rect[0], rect[1] - rect[3])
        print("rect top mid:", (round(top_mid[0], 1), round(top_mid[1], 1)),
              "bottom mid:", (round(bot_mid[0], 1), round(bot_mid[1], 1)))
        # quad edge midpoints, labelled by stored order
        pairs = {"left(3-0)": (3, 0), "bot(0-1)": (0, 1),
                 "right(1-2)": (1, 2), "top(2-3)": (2, 3)}
        for name, (a, b) in pairs.items():
            print(f"  quad edge {name}: "
                  f"({(q[a][0]+q[b][0])/2:.1f},{(q[a][1]+q[b][1])/2:.1f})")
        # what zone does hovering the rect's TOP strip return?
        z_top = ct._refboard_zone(rect, top_mid[0], top_mid[1], True, True)
        z_bot = ct._refboard_zone(rect, bot_mid[0], bot_mid[1], True, True)
        print("zone@rect-top:", z_top, " zone@rect-bottom:", z_bot)

    return 0


sys.exit(main())
