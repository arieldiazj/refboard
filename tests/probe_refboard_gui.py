"""GUI probe for Refboard drawing. Run with a real (windowed) Blender:

    blender.exe --factory-startup --python tests/probe_refboard_gui.py

Adds a synthetic screen-space Refboard, forces a redraw, screenshots the
window, checks for the image's pixels in the capture, dumps diagnostics to
_refboard_probe_out.txt, then quits.
"""
import os
import traceback

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "_refboard_probe_out.txt")
SHOT = os.path.join(HERE, "_refboard_shot.png")


def log(*a):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(" ".join(str(x) for x in a) + "\n")
    print("[PROBE]", *a, flush=True)


def step1():
    try:
        open(OUT, "w").close()
        try:
            bpy.ops.preferences.addon_enable(module="refboard")
        except Exception as e:
            log("addon_enable exc:", repr(e))
        import refboard as ct
        log("handler:", ct._REFBOARD_HANDLER)

        sc = bpy.context.scene
        img = bpy.data.images.new("probe_red", 64, 64, alpha=True)
        img.pixels[:] = [1.0, 0.0, 0.0, 1.0] * (64 * 64)
        item = sc.refboard_items.add()
        item.image = img
        item.pos = (0.5, 0.5)
        sc.refboard_selected = 0
        log("items:", len(sc.refboard_items))

        # Simulate the cursor sitting in the top-right corner's rotate zone
        # so the corner arc renders too.
        try:
            for w in bpy.context.window_manager.windows:
                for a in (w.screen.areas if w.screen else ()):
                    if a.type == 'VIEW_3D':
                        for rg in a.regions:
                            if rg.type == 'WINDOW':
                                ct._refboard_hover = (rg.as_pointer(), 1)
        except Exception:
            log("hover exc:", traceback.format_exc())

        try:
            ts, fs = ct._refboard_shaders()
            log("tex shader:", ts, "needs_mvp:", ct._REFBOARD_TEX_SHADER_NEEDS_MVP)
            log("flat shader:", fs, "needs_mvp:", ct._REFBOARD_FLAT_SHADER_NEEDS_MVP)
            log("smooth shader:", ct._REFBOARD_SMOOTH_SHADER,
                "needs_mvp:", ct._REFBOARD_SMOOTH_SHADER_NEEDS_MVP)
        except Exception:
            log("shader exc:", traceback.format_exc())

        try:
            tex = ct.gpu.texture.from_image(img)
            log("gpu texture:", tex)
        except Exception:
            log("tex exc:", traceback.format_exc())

        try:
            log("mvp:", ct._refboard_mvp())
        except Exception:
            log("mvp exc:", traceback.format_exc())

        for w in bpy.context.window_manager.windows:
            if w.screen:
                for a in w.screen.areas:
                    a.tag_redraw()
        log("step1 done")
    except Exception:
        log("step1 exc:", traceback.format_exc())
    return None


def step2():
    try:
        for w in bpy.context.window_manager.windows:
            if w.screen:
                for a in w.screen.areas:
                    a.tag_redraw()
        bpy.ops.screen.screenshot(filepath=SHOT)
        log("screenshot ok")
    except Exception:
        log("shot exc:", traceback.format_exc())
    return None


def step3():
    try:
        if os.path.isfile(SHOT):
            shot = bpy.data.images.load(SHOT)
            import numpy as np
            px = np.empty(len(shot.pixels), dtype=np.float32)
            shot.pixels.foreach_get(px)
            px = px.reshape(-1, 4)
            red = ((px[:, 0] > 0.7) & (px[:, 1] < 0.3) &
                   (px[:, 2] < 0.3)).sum()
            log("red pixels in screenshot:", int(red))
            orange = ((px[:, 0] > 0.7) & (px[:, 1] > 0.25) &
                      (px[:, 1] < 0.75) & (px[:, 2] < 0.3)).sum()
            log("orange pixels in screenshot:", int(orange))
        else:
            log("no screenshot file")
    except Exception:
        log("analyze exc:", traceback.format_exc())
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(step1, first_interval=1.5)
bpy.app.timers.register(step2, first_interval=3.0)
bpy.app.timers.register(step3, first_interval=4.5)
