"""Colorist Halation - warm highlight bloom, the way film actually does it.

When bright light hits film, some of it scatters back through the emulsion and the
base and re-exposes the area around the highlight. Because it passes through the
dye layers, that re-exposure skews red-orange. The result is the soft warm glow you
see ringing practical lights, windows, and speculars in real footage.

This node does it correctly, not as a global bloom:
  1. Work in linear light, where scattered light actually adds.
  2. Isolate only the true highlights with a soft threshold.
  3. Spread them with a wide, soft blur (two radii for a natural falloff).
  4. Tint that glow red-orange, the color real halation takes.
  5. Screen it back over the image so it lifts the surround without clipping.

Apply it BEFORE grain: halation is image formation, grain sits on top. Torch only,
batch-safe for video.

Controls:
  threshold  how bright a pixel must be to bloom (lower catches more highlights).
  strength   how strong the glow is.
  radius     how far the glow spreads, in pixels.
"""

import torch
import torch.nn.functional as F

# Red-orange tint of real halation.
_TINT = (1.0, 0.30, 0.08)


def _srgb_to_linear(x):
    return torch.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(x):
    x = x.clamp(min=0.0)
    return torch.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1.0 / 2.4) - 0.055)


class ColoristHalation:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "threshold": ("FLOAT", {"default": 0.5, "min": 0.0, "max": 1.0, "step": 0.01}),
                "strength": ("FLOAT", {"default": 1.4, "min": 0.0, "max": 4.0, "step": 0.05}),
                "radius": ("FLOAT", {"default": 18.0, "min": 1.0, "max": 128.0, "step": 1.0}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "apply_halation"
    CATEGORY = "image/postprocessing"

    @staticmethod
    def _blur(x, sigma):
        """Separable Gaussian blur of [B,1,H,W]."""
        rad = max(1, int(round(sigma * 3.0)))
        idx = torch.arange(-rad, rad + 1, dtype=torch.float32, device=x.device)
        k = torch.exp(-(idx * idx) / (2.0 * sigma * sigma))
        k = k / k.sum()
        x = F.conv2d(x, k.view(1, 1, -1, 1), padding=(rad, 0))
        x = F.conv2d(x, k.view(1, 1, 1, -1), padding=(0, rad))
        return x

    def apply_halation(self, image, threshold, strength, radius):
        lin = _srgb_to_linear(image)                                   # [B,H,W,3] linear
        luma = 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]

        # Soft highlight mask: zero below threshold, rising to 1 at white.
        thr = float(threshold)
        mask = ((luma - thr) / max(1e-3, 1.0 - thr)).clamp(0.0, 1.0) ** 2
        h = (luma * mask).unsqueeze(1)                                 # [B,1,H,W]

        # Wide soft spread, two radii for a natural falloff.
        r = float(radius)
        hb = 0.6 * self._blur(h, r) + 0.4 * self._blur(h, r * 2.5)
        hb = hb.squeeze(1).unsqueeze(-1)                              # [B,H,W,1]

        tint = torch.tensor(_TINT, device=image.device).view(1, 1, 1, 3)
        glow = (hb * tint * float(strength)).clamp(0.0, 1.0)

        out = 1.0 - (1.0 - lin) * (1.0 - glow)                        # screen, in linear light
        return (_linear_to_srgb(out).clamp(0.0, 1.0).to(image.dtype),)


NODE_CLASS_MAPPINGS = {"ColoristHalation": ColoristHalation}
NODE_DISPLAY_NAME_MAPPINGS = {"ColoristHalation": "Colorist Halation"}
