"""Colorist Film Grain - a 35mm-faithful film grain node for ComfyUI.

This models the way real color negative grain behaves, instead of laying a soft
noise overlay on the frame:

  1. Per-channel grain (three emulsion layers). Color film has independent grain
     in its red, green and blue records, and the blue layer is the grainiest. The
     grain here is generated per channel with that weighting, so it has the faint
     chromatic shimmer of real stock rather than flat gray speckle.
  2. Crisp band-pass clumps. The noise is built at full resolution and shaped with
     a difference of Gaussians (a fine blur minus a broader one), which removes the
     large-scale blotch and leaves the even, sharp sparkle of a real film spectrum.
     The blue layer's clumps run larger, the red layer's finer, like real stock.
  3. Signal dependent amplitude. Film granularity peaks in the midtones and is
     present everywhere, never zero, rolling off but not vanishing in the deep
     shadows and the bright highlights. The curve is the physical sqrt(I*(1-I))
     per channel, with a floor. This is the single biggest difference between grain
     that reads as film and grain that reads as added in post.
  4. Additive. Grain is signed density fluctuation added to the image, not a
     contrasty overlay.
  5. Temporally coherent. The grain evolves frame to frame (real film grain is
     different every frame), with a constant power crossfade so it never pulses.
     grain_motion runs from frozen to fresh.

Torch only. Loads with a restart, runs a whole video batch.

The nine controls:
  grain_size   clump size. Smaller is finer 35mm, larger is coarser, older stock.
  strength     overall grain amount.
  shadow_bias  tilts the amplitude curve. 0 is the physical midtone peak, higher
               pushes grain toward the shadows.
  luma_amt     the achromatic (shared) part of the grain.
  chroma_amt   the chromatic (per-channel) part of the grain.
  blend_mode   add is the film-true path; overlay and soft_light are stylistic.
  seed         repeatable grain.
  grain_motion 0 frozen, 1 fresh every frame, middle alive but coherent.
"""

import math

import torch
import torch.nn.functional as F

_R709 = (0.2126, 0.7152, 0.0722)

# Relative grain of the three emulsion layers. Blue is grainiest, red finest.
_CHANNEL_GRAIN = (0.85, 1.0, 1.35)
# Relative grain clump size per layer. Blue clumps are larger, red finer.
_CHANNEL_SIZE = (0.85, 1.0, 1.3)
# Difference-of-Gaussians strength: subtract a broader blur to remove the
# low-frequency blotch, leaving the fine band-pass (Wiener) spectrum of real film.
# Lower = smoother (more 35mm), higher = crunchier (more rough 16mm).
_BANDPASS_MIX = 0.35
# Clump size in pixels = _SIGMA_BASE + _SIGMA_SLOPE * grain_size. Lower base = finer
# grain overall (35mm reads finer than 16mm at the same grain_size).
_SIGMA_BASE = 0.18
_SIGMA_SLOPE = 0.35

_BASE_SIGMA = 0.12   # grain std at strength 1, midtone, before strength scaling
_AMP_FLOOR = 0.20    # grain never drops below this fraction (present everywhere)


class ColoristFilmGrain:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": ("IMAGE",),
                "grain_size": ("FLOAT", {"default": 0.65, "min": 0.5, "max": 4.0, "step": 0.05}),
                "strength": ("FLOAT", {"default": 0.15, "min": 0.0, "max": 1.0, "step": 0.01}),
                "shadow_bias": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01}),
                "luma_amt": ("FLOAT", {"default": 0.85, "min": 0.0, "max": 1.0, "step": 0.01}),
                "chroma_amt": ("FLOAT", {"default": 0.25, "min": 0.0, "max": 1.0, "step": 0.01}),
                "blend_mode": (["add", "overlay", "soft_light"],),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "grain_motion": ("FLOAT", {"default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01}),
            }
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "apply_grain"
    CATEGORY = "image/postprocessing"

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _noise_volume(channels, t, h, w, motion, generator, device):
        """Full-resolution noise [T,channels,H,W] that evolves over time.

        A small stack of random keyframes is crossfaded across the clip with a
        constant power (cos/sin) blend, so the grain drifts without its strength
        pulsing. motion 0 = one frozen plate, 1 = a fresh plate every frame.
        """
        if motion <= 1e-4 or t <= 1:
            keys = 1
        else:
            keys = int(max(2, min(t, round(t * motion))))

        vol = torch.randn((keys, channels, h, w), generator=generator, device=device)
        if keys == 1:
            return vol.expand(t, channels, h, w)

        pos = torch.linspace(0.0, keys - 1, t, device=device)
        lo = torch.floor(pos).long().clamp(0, keys - 1)
        hi = torch.clamp(lo + 1, max=keys - 1)
        frac = (pos - lo.float()).view(t, 1, 1, 1)
        w_lo = torch.cos(frac * (math.pi / 2.0))
        w_hi = torch.sin(frac * (math.pi / 2.0))
        return vol[lo] * w_lo + vol[hi] * w_hi

    @staticmethod
    def _blur_perchannel(x, sigmas):
        """Separable Gaussian blur of [T,3,H,W] with a different sigma per channel.

        One grouped convolution carries all three per-layer clump sizes at once.
        """
        rad = max(1, int(round(max(sigmas) * 3.0)))
        rows = []
        for s in sigmas:
            if s < 0.25:
                k = torch.zeros(2 * rad + 1, dtype=torch.float32)
                k[rad] = 1.0
            else:
                idx = torch.arange(-rad, rad + 1, dtype=torch.float32)
                k = torch.exp(-(idx * idx) / (2.0 * s * s))
                k = k / k.sum()
            rows.append(k)
        kern = torch.stack(rows, 0).to(x.device)        # [3, 2rad+1]
        kh = kern.view(3, 1, -1, 1)
        kw = kern.view(3, 1, 1, -1)
        x = F.conv2d(x, kh, padding=(rad, 0), groups=3)
        x = F.conv2d(x, kw, padding=(0, rad), groups=3)
        return x

    # -------------------------------------------------------------------- worker

    def apply_grain(self, image, grain_size, strength, shadow_bias, luma_amt,
                    chroma_amt, blend_mode, seed, grain_motion=0.6):
        device = image.device
        t, h, w, _ = image.shape
        gen = torch.Generator(device="cpu").manual_seed(int(seed) & 0xFFFFFFFFFFFFFFFF)
        # noise is generated on CPU for repeatability, then moved to the image device
        ndev = "cpu"

        m = float(grain_motion)
        shared = self._noise_volume(1, t, h, w, m, gen, ndev)   # [T,1,H,W] achromatic
        indep = self._noise_volume(3, t, h, w, m, gen, ndev)    # [T,3,H,W] per channel

        chanw = torch.tensor(_CHANNEL_GRAIN, device=ndev).view(1, 3, 1, 1)
        shared3 = shared.repeat(1, 3, 1, 1) * float(luma_amt)   # achromatic luminance grain
        chroma3 = chanw * indep * float(chroma_amt)             # per-channel color grain

        # Band-pass clumps (difference of Gaussians): blur to a sub-pixel clump size,
        # then subtract a broader blur so the low-frequency blotch is removed, leaving
        # the fine even sparkle of a real film grain spectrum. The luminance grain is
        # blurred at one uniform size so it stays achromatic (true B&W at chroma 0),
        # while the color grain gets the per-layer sizes (blue larger, red finer).
        sigma_px = _SIGMA_BASE + _SIGMA_SLOPE * float(grain_size)

        def bandpass(x, sigmas):
            return self._blur_perchannel(x, sigmas) - _BANDPASS_MIX * self._blur_perchannel(x, [s * 2.5 for s in sigmas])

        mean_size = sum(_CHANNEL_SIZE) / 3.0
        grain = bandpass(shared3, [sigma_px * mean_size] * 3) \
            + bandpass(chroma3, [sigma_px * s for s in _CHANNEL_SIZE])
        grain = grain / (grain.std() + 1e-6)
        grain = grain.to(device)

        # Signal-dependent amplitude, per channel. Physical midtone peak with a
        # floor so grain is present in shadows and highlights too.
        img_c = image.permute(0, 3, 1, 2)                       # [T,3,H,W]
        sqrt_curve = 2.0 * torch.sqrt((img_c * (1.0 - img_c)).clamp(min=0.0))   # 0..1, peak at 0.5
        amp = _AMP_FLOOR + (1.0 - _AMP_FLOOR) * sqrt_curve
        # shadow_bias tilts grain toward the shadows without removing the floor.
        amp = amp * (1.0 - float(shadow_bias) * (2.0 * img_c - 1.0)).clamp(min=0.0)

        g = grain * (_BASE_SIGMA * float(strength)) * amp       # [T,3,H,W] signed grain

        if blend_mode == "add":
            out = img_c + g
        elif blend_mode == "overlay":
            layer = (0.5 + g).clamp(0.0, 1.0)
            out = torch.where(img_c <= 0.5,
                              2.0 * img_c * layer,
                              1.0 - 2.0 * (1.0 - img_c) * (1.0 - layer))
        else:  # soft_light (Pegtop)
            layer = (0.5 + g).clamp(0.0, 1.0)
            gg = torch.where(img_c <= 0.25, ((16.0 * img_c - 12.0) * img_c + 4.0) * img_c, torch.sqrt(img_c))
            out = torch.where(layer <= 0.5,
                              img_c - (1.0 - 2.0 * layer) * img_c * (1.0 - img_c),
                              img_c + (2.0 * layer - 1.0) * (gg - img_c))

        out = out.permute(0, 2, 3, 1).clamp(0.0, 1.0)
        return (out.to(image.dtype),)


NODE_CLASS_MAPPINGS = {"ColoristFilmGrain": ColoristFilmGrain}
NODE_DISPLAY_NAME_MAPPINGS = {"ColoristFilmGrain": "Colorist Film Grain"}
