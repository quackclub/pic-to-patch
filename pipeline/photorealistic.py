#!/usr/bin/env python3
"""
Advanced post-processing pipeline that makes inkstitch renders look like
photographs of real embroidered patches.

Pipeline:
  1. Extract patch mask from white background
  2. Compute per-pixel normal map from stitch texture gradients
  3. Estimate local thread direction via structure tensor
  4. Apply Blinn-Phong shading with Kajiya-Kay anisotropic specular
  5. Add per-thread micro-highlight variation
  6. Generate felt/twill backing fabric texture
  7. Create realistic drop shadow (contact + cast)
  8. Simulate merrow/overlock edge border
  9. Add patch thickness bevel (embossed raised edge)
  10. Composite everything
  11. Photographic finishing: DOF, color grade, vignette, grain
"""

import sys
import numpy as np
from pathlib import Path
from PIL import Image, ImageFilter, ImageEnhance, ImageDraw
from scipy import ndimage
from scipy.ndimage import gaussian_filter, uniform_filter


# ─────────────────────────────────────────────
# 1. Mask extraction
# ─────────────────────────────────────────────

def extract_patch_mask(img_arr, threshold=235):
    """
    Extract a mask of the embroidered region vs white background.
    Returns float mask [0, 1] with anti-aliased soft edges.
    """
    is_bg = np.all(img_arr[:, :, :3] > threshold, axis=2)
    mask = (~is_bg).astype(np.float32)

    # Close small holes inside the patch
    mask = ndimage.binary_closing(mask > 0.5, iterations=3).astype(np.float32)
    # Dilate slightly to catch anti-aliased edge pixels
    mask = ndimage.binary_dilation(mask > 0.5, iterations=1).astype(np.float32)

    # Soft edge via gaussian blur
    mask_soft = gaussian_filter(mask, sigma=1.2)
    return np.clip(mask_soft, 0, 1)


# ─────────────────────────────────────────────
# 2. Normal map computation
# ─────────────────────────────────────────────

def compute_normal_map(img_arr, mask, strength=2.0):
    """
    Derive per-pixel surface normals from stitch texture luminance gradients.
    Multi-scale: fine captures thread ridges, medium captures stitch rows.
    """
    lum = (0.299 * img_arr[:, :, 0] +
           0.587 * img_arr[:, :, 1] +
           0.114 * img_arr[:, :, 2]).astype(np.float64)

    # Fine scale - individual thread ridges
    gx_fine = ndimage.sobel(lum, axis=1)
    gy_fine = ndimage.sobel(lum, axis=0)

    # Medium scale - stitch row structure
    lum_med = gaussian_filter(lum, sigma=1.2)
    gx_med = ndimage.sobel(lum_med, axis=1)
    gy_med = ndimage.sobel(lum_med, axis=0)

    # Coarse scale - broad curvature
    lum_coarse = gaussian_filter(lum, sigma=3.0)
    gx_coarse = ndimage.sobel(lum_coarse, axis=1)
    gy_coarse = ndimage.sobel(lum_coarse, axis=0)

    gx = 0.5 * gx_fine + 0.35 * gx_med + 0.15 * gx_coarse
    gy = 0.5 * gy_fine + 0.35 * gy_med + 0.15 * gy_coarse

    gx *= strength
    gy *= strength

    h, w = lum.shape
    normals = np.zeros((h, w, 3), dtype=np.float64)
    normals[:, :, 0] = -gx
    normals[:, :, 1] = -gy
    normals[:, :, 2] = 255.0

    length = np.sqrt(np.sum(normals ** 2, axis=2, keepdims=True))
    normals /= np.maximum(length, 1e-8)
    normals *= mask[:, :, np.newaxis]

    return normals


# ─────────────────────────────────────────────
# 3. Thread direction estimation
# ─────────────────────────────────────────────

def estimate_thread_direction(img_arr, mask, block_size=12):
    """
    Estimate local thread direction via structure tensor analysis.
    Returns angle map in radians for anisotropic specular.
    """
    lum = (0.299 * img_arr[:, :, 0] +
           0.587 * img_arr[:, :, 1] +
           0.114 * img_arr[:, :, 2]).astype(np.float64)

    gx = ndimage.sobel(lum, axis=1)
    gy = ndimage.sobel(lum, axis=0)

    sigma = block_size / 2.0
    Jxx = gaussian_filter(gx * gx, sigma=sigma)
    Jxy = gaussian_filter(gx * gy, sigma=sigma)
    Jyy = gaussian_filter(gy * gy, sigma=sigma)

    # Dominant gradient direction
    angle = 0.5 * np.arctan2(2.0 * Jxy, Jxx - Jyy + 1e-10)
    # Thread runs perpendicular to gradient
    thread_angle = angle + np.pi / 2.0

    # Also compute anisotropy strength (how directional the texture is)
    trace = Jxx + Jyy + 1e-10
    diff = np.sqrt((Jxx - Jyy) ** 2 + 4 * Jxy ** 2)
    anisotropy = diff / trace  # 0 = isotropic, 1 = perfectly directional

    return thread_angle, anisotropy


# ─────────────────────────────────────────────
# 4. Blinn-Phong + anisotropic shading
# ─────────────────────────────────────────────

def apply_lighting(img_arr, normals, mask, thread_angle, anisotropy,
                   light_dir=(0.25, -0.35, 0.90),
                   ambient=0.55, diffuse_strength=0.35,
                   specular_strength=0.12, shininess=18.0,
                   aniso_strength=0.08, aniso_shininess=30.0):
    """
    Apply Blinn-Phong shading with Kajiya-Kay anisotropic specular.

    The key insight: we DON'T want to darken the patch too much. Real embroidery
    is bright and saturated under normal lighting. The shading should add
    subtle variation and highlights, not dramatically change overall brightness.

    We use a "detail lighting" approach:
      final = original * (ambient + diffuse) + specular_white
    where ambient+diffuse averages close to 1.0 across the image.
    """
    result = img_arr.astype(np.float64).copy()

    light = np.array(light_dir, dtype=np.float64)
    light /= np.linalg.norm(light)

    view = np.array([0.0, 0.0, 1.0])
    half_vec = light + view
    half_vec /= np.linalg.norm(half_vec)

    # --- Diffuse ---
    n_dot_l = (normals[:, :, 0] * light[0] +
               normals[:, :, 1] * light[1] +
               normals[:, :, 2] * light[2])
    n_dot_l = np.clip(n_dot_l, 0, 1)

    # Wrap diffuse to soften shadows (half-lambert)
    diffuse = diffuse_strength * (n_dot_l * 0.5 + 0.5)

    # --- Isotropic specular ---
    n_dot_h = (normals[:, :, 0] * half_vec[0] +
               normals[:, :, 1] * half_vec[1] +
               normals[:, :, 2] * half_vec[2])
    n_dot_h = np.clip(n_dot_h, 0, 1)
    specular_iso = specular_strength * np.power(n_dot_h, shininess)

    # --- Anisotropic specular (Kajiya-Kay) ---
    tangent_x = np.cos(thread_angle)
    tangent_y = np.sin(thread_angle)

    t_dot_h = tangent_x * half_vec[0] + tangent_y * half_vec[1]
    sin_th = np.sqrt(np.clip(1.0 - t_dot_h ** 2, 0, 1))
    # Modulate by local anisotropy - only apply aniso spec where texture is directional
    specular_aniso = aniso_strength * np.power(sin_th, aniso_shininess) * anisotropy

    # --- Secondary broad specular (satin sheen on lighter regions) ---
    # Lighter areas (white text, bright embroidery) have more specular
    lum = np.mean(result[:, :, :3], axis=2) / 255.0
    brightness_boost = np.clip(lum - 0.4, 0, 0.6) / 0.6  # ramp from 0.4 to 1.0
    spec_broad = 0.06 * np.power(n_dot_h, 8.0) * brightness_boost

    # --- Combine ---
    # Multiplicative lighting (affects color)
    color_light = ambient + diffuse
    # Ensure average is near 1.0 to preserve original brightness
    color_light = np.clip(color_light, 0.35, 1.5)

    # Additive specular (white highlight)
    spec_total = (specular_iso + specular_aniso + spec_broad) * mask

    # Apply
    for c in range(3):
        channel = result[:, :, c]
        lit = channel * color_light + spec_total * 220.0
        result[:, :, c] = channel * (1.0 - mask) + lit * mask

    return np.clip(result, 0, 255)


# ─────────────────────────────────────────────
# 5. Per-thread micro-highlights
# ─────────────────────────────────────────────

def compute_ambient_occlusion(img_arr, mask, radius=2.0, strength=0.15):
    """
    Approximate screen-space ambient occlusion from stitch texture.
    Stitch valleys (between thread rows) are slightly darker.
    Computed by comparing local brightness to neighborhood average.
    """
    lum = np.mean(img_arr[:, :, :3], axis=2)
    local_avg = gaussian_filter(lum, sigma=radius)
    ao = np.clip((local_avg - lum) / (local_avg + 1e-8), 0, 1) * strength
    ao *= mask
    return ao


def add_thread_microhighlights(img_arr, normals, mask, thread_angle, intensity=0.05):
    """
    Individual threads catch light slightly differently.
    Creates fine-grained brightness variation aligned with thread direction.
    Also adds subtle shimmer for thread luster.
    """
    h, w = img_arr.shape[:2]
    noise = np.random.normal(0, 1, (h, w))

    # Directional blur kernels
    noise_along = gaussian_filter(noise, sigma=[0.3, 3.0])
    noise_across = gaussian_filter(noise, sigma=[3.0, 0.3])

    # Blend using thread angle
    cos_a = np.abs(np.cos(thread_angle))
    sin_a = np.abs(np.sin(thread_angle))
    total = cos_a + sin_a + 1e-8
    directional = (noise_along * cos_a + noise_across * sin_a) / total

    highlight = directional * intensity * mask
    result = img_arr.copy()
    for c in range(3):
        result[:, :, c] *= (1.0 + highlight)

    # Fine per-pixel shimmer for thread luster
    shimmer = np.random.normal(0, 0.015, (h, w))
    shimmer = gaussian_filter(shimmer, sigma=0.3)
    for c in range(3):
        result[:, :, c] *= (1.0 + shimmer * mask)

    return np.clip(result, 0, 255)


# ─────────────────────────────────────────────
# 6. Fabric backing texture
# ─────────────────────────────────────────────

def generate_fabric_texture(width, height, color=(35, 35, 40), weave_scale=3):
    """
    Generate realistic felt/twill fabric backing.
    Multi-octave noise with diagonal weave pattern.
    """
    arr = np.full((height, width, 3), color, dtype=np.float64)

    y_idx = np.arange(height)[:, None]
    x_idx = np.arange(width)[None, :]

    # Twill diagonal weave
    twill1 = np.sin(2 * np.pi * (x_idx + y_idx) / weave_scale) * 0.035
    twill2 = np.sin(2 * np.pi * (x_idx - y_idx) / (weave_scale * 1.3)) * 0.02
    twill3 = np.sin(2 * np.pi * (x_idx * 0.7 + y_idx * 1.3) / (weave_scale * 2)) * 0.015
    arr *= (1.0 + twill1 + twill2 + twill3)[:, :, np.newaxis]

    # Fine fiber noise
    noise_fine = np.random.normal(0, 2.0, (height, width, 3))
    # Medium texture clumps
    noise_med_small = np.random.normal(0, 1.2, (height // 2 + 1, width // 2 + 1, 3))
    noise_med = np.repeat(np.repeat(noise_med_small, 2, axis=0), 2, axis=1)[:height, :width, :]
    noise_med = gaussian_filter(noise_med, sigma=1.0)
    # Coarse color drift
    noise_coarse_small = np.random.normal(0, 0.8, (height // 6 + 1, width // 6 + 1, 3))
    noise_coarse = np.repeat(np.repeat(noise_coarse_small, 6, axis=0), 6, axis=1)[:height, :width, :]
    noise_coarse = gaussian_filter(noise_coarse, sigma=3.0)

    arr += noise_fine + noise_med + noise_coarse

    # Subtle large-scale brightness variation (fabric isn't perfectly uniform)
    var_small = np.random.normal(0, 0.015, (height // 12 + 1, width // 12 + 1))
    variation = np.repeat(np.repeat(var_small, 12, axis=0), 12, axis=1)[:height, :width]
    variation = gaussian_filter(variation, sigma=6.0)
    arr *= (1.0 + variation[:, :, np.newaxis])

    return np.clip(arr, 0, 255).astype(np.uint8)


# ─────────────────────────────────────────────
# 7. Shadow
# ─────────────────────────────────────────────

def create_patch_shadow(mask, offset=(5, 6), blur_radius=10, opacity=0.55):
    """
    Realistic two-layer shadow:
      - Contact shadow: tight, dark, right at the edge
      - Cast shadow: offset, soft, diffuse
    """
    h, w = mask.shape

    # Cast shadow: shift the mask by offset and blur
    shifted = ndimage.shift(mask, (offset[1], offset[0]), order=1, mode='constant', cval=0)
    cast = gaussian_filter(shifted, sigma=blur_radius) * opacity

    # Contact shadow: unshifted tight edge glow
    # Expand mask slightly, subtract original, blur tightly
    dilated = gaussian_filter(mask, sigma=2.0)
    contact_ring = np.clip(dilated - mask * 0.9, 0, 1)
    contact = gaussian_filter(contact_ring, sigma=2.5) * 0.4

    # Combine
    shadow = np.maximum(cast, contact)
    # Don't darken the patch interior
    shadow *= np.clip(1.0 - mask, 0, 1)

    return np.clip(shadow, 0, 1)


# ─────────────────────────────────────────────
# 8. Merrow edge
# ─────────────────────────────────────────────

def create_merrow_edge(mask, thickness=3):
    """
    Simulate overlock/merrow stitch border around patch edge.
    Returns (edge_mask, edge_normals) for 3D stitched border look.
    """
    h, w = mask.shape

    # Get edge band via morphological gradient
    hard_mask = (mask > 0.5).astype(np.float32)
    dilated = ndimage.binary_dilation(hard_mask > 0.5, iterations=thickness).astype(np.float32)
    eroded = ndimage.binary_erosion(hard_mask > 0.5, iterations=max(1, thickness // 2)).astype(np.float32)
    edge_band = np.clip(dilated - eroded, 0, 1)

    # Anti-alias the edge band
    edge_band = gaussian_filter(edge_band, sigma=0.6)

    # Add stitch texture pattern along the edge
    y_idx = np.arange(h)[:, None].astype(np.float64)
    x_idx = np.arange(w)[None, :].astype(np.float64)

    # Distance from center for radial stitch direction
    cy, cx = h / 2.0, w / 2.0
    dy = y_idx - cy
    dx = x_idx - cx
    angle = np.arctan2(dy, dx)

    # Stitch pattern follows the edge circumferentially
    # Use angle to create perpendicular stitches
    dist = np.sqrt(dx ** 2 + dy ** 2)
    stitch_freq = dist * 0.15  # scale frequency by radius
    stitch_pattern = np.sin(stitch_freq + angle * 25) * 0.12 + 0.88
    stitch_pattern2 = np.cos(stitch_freq * 1.7 + angle * 18) * 0.06 + 0.94

    edge_textured = edge_band * stitch_pattern * stitch_pattern2

    return edge_textured, edge_band


# ─────────────────────────────────────────────
# 9. Patch thickness bevel
# ─────────────────────────────────────────────

def create_edge_bevel(mask, bevel_width=6, light_dir=(0.3, -0.4)):
    """
    Create a bevel/emboss effect at the patch edge to simulate thickness.
    The patch is raised ~1-2mm above the fabric, creating a lit top edge
    and shadowed bottom edge.
    """
    # Compute distance from edge (inward)
    hard_mask = (mask > 0.5).astype(np.float32)
    dist = ndimage.distance_transform_edt(hard_mask)
    dist_outside = ndimage.distance_transform_edt(1 - hard_mask)

    # Bevel height profile: ramps up at edge, flat in interior
    bevel_height = np.clip(dist / bevel_width, 0, 1)
    # Also slight ramp outside for the outer edge
    bevel_height_out = np.clip(1.0 - dist_outside / (bevel_width * 0.5), 0, 1) * (1 - hard_mask)

    height = bevel_height + bevel_height_out

    # Compute lighting from height map
    gx = ndimage.sobel(height, axis=1)
    gy = ndimage.sobel(height, axis=0)

    # Directional lighting
    lx, ly = light_dir
    bevel_light = -(gx * lx + gy * ly)

    # Normalize to [-1, 1] range
    max_val = max(np.abs(bevel_light).max(), 1e-8)
    bevel_light = bevel_light / max_val

    # Only apply near edges
    edge_proximity = np.clip(1.0 - dist / (bevel_width * 1.5), 0, 1) * hard_mask
    edge_proximity += np.clip(1.0 - dist_outside / (bevel_width * 0.8), 0, 1) * (1 - hard_mask)

    bevel_light *= edge_proximity

    return bevel_light


def create_inner_relief(img_arr, mask, light_dir=(0.3, -0.4), strength=0.08):
    """
    Detect color boundaries within the patch (where different stitch sections
    meet) and add subtle height relief at those boundaries. In real embroidery,
    the NASA text sits slightly above the blue fill, the chevron overlaps, etc.
    """
    h, w = img_arr.shape[:2]

    # Detect edges within the patch using color gradient magnitude
    # Use all 3 channels for better boundary detection
    edges = np.zeros((h, w), dtype=np.float64)
    for c in range(3):
        gx = ndimage.sobel(img_arr[:, :, c], axis=1)
        gy = ndimage.sobel(img_arr[:, :, c], axis=0)
        edges += np.sqrt(gx ** 2 + gy ** 2)
    edges /= 3.0

    # Threshold to find significant color boundaries (not just stitch texture)
    # Smooth to get section-level boundaries, not individual thread edges
    edges_smooth = gaussian_filter(edges, sigma=1.5)

    # Normalize
    edge_max = np.percentile(edges_smooth[mask > 0.5], 95) if np.any(mask > 0.5) else 1.0
    edges_norm = np.clip(edges_smooth / (edge_max + 1e-8), 0, 1)

    # Create height map: sections have flat heights, boundaries have transitions
    # Use edge magnitude as a proxy for height discontinuity
    height = gaussian_filter(edges_norm, sigma=2.0) * mask

    # Compute directional lighting on this height map
    gx = ndimage.sobel(height, axis=1)
    gy = ndimage.sobel(height, axis=0)
    lx, ly = light_dir
    relief = -(gx * lx + gy * ly) * strength * mask

    return relief


# ─────────────────────────────────────────────
# 10-11. Photographic effects
# ─────────────────────────────────────────────

def add_vignette(img_arr, strength=0.22, radius=0.65):
    """Photographic vignette - darkens corners."""
    h, w = img_arr.shape[:2]
    y = np.linspace(-1, 1, h)[:, None]
    x = np.linspace(-1, 1, w)[None, :]
    dist = np.sqrt(x * x + y * y)
    vignette = 1.0 - strength * np.clip((dist - radius) / (1.4 - radius), 0, 1) ** 1.5
    return img_arr * vignette[:, :, np.newaxis]


def add_film_grain(img_arr, strength=3.0):
    """Photographic film grain with realistic grain size."""
    h, w = img_arr.shape[:2]
    # Luminance-dependent grain (stronger in shadows)
    lum = np.mean(img_arr[:, :, :3], axis=2)
    grain_strength = strength * (1.0 + 0.3 * (1.0 - lum / 255.0))

    grain = np.random.normal(0, 1, (h, w)) * grain_strength
    grain = gaussian_filter(grain, sigma=0.4)

    result = img_arr + grain[:, :, np.newaxis]
    return np.clip(result, 0, 255)


def add_depth_of_field(img_arr, mask, max_blur=1.3):
    """Subtle DOF: patch center sharp, frame edges soft."""
    h, w = img_arr.shape[:2]

    y_coords, x_coords = np.where(mask > 0.5)
    if len(y_coords) == 0:
        return img_arr
    cx, cy = int(np.mean(x_coords)), int(np.mean(y_coords))

    y = np.arange(h)[:, None]
    x = np.arange(w)[None, :]
    dist = np.sqrt(((x - cx) / w * 2) ** 2 + ((y - cy) / h * 2) ** 2)
    blur_t = np.clip((dist - 0.35) / 0.65, 0, 1) ** 1.5

    blurred = np.stack([
        gaussian_filter(img_arr[:, :, c], sigma=max_blur)
        for c in range(img_arr.shape[2])
    ], axis=2)

    blend = blur_t[:, :, np.newaxis]
    return img_arr * (1 - blend) + blurred * blend


def color_grade(img_arr, warmth=0.02, contrast=1.05, saturation=1.10):
    """Subtle photographic color grading."""
    result = img_arr.copy()

    # Warmth
    result[:, :, 0] *= (1.0 + warmth)
    result[:, :, 1] *= (1.0 + warmth * 0.2)
    result[:, :, 2] *= (1.0 - warmth * 0.4)

    # S-curve contrast (gentle)
    mid = 128.0
    result = mid + (result - mid) * contrast

    # Saturation boost (embroidery is vivid)
    gray = np.mean(result[:, :, :3], axis=2, keepdims=True)
    result[:, :, :3] = gray + (result[:, :, :3] - gray) * saturation

    return np.clip(result, 0, 255)


# ─────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────

def postprocess_photorealistic(input_path, output_path,
                                fabric_color=(35, 35, 40),
                                padding=55,
                                light_dir=(0.25, -0.35, 0.90)):
    """
    Full photorealistic post-processing pipeline.
    """
    print(f"Loading {input_path}...")
    stitch_img = Image.open(input_path)
    stitch_arr = np.array(stitch_img)[:, :, :3].astype(np.float64)
    h, w = stitch_arr.shape[:2]

    print("  [1/13] Extracting patch mask...")
    mask = extract_patch_mask(stitch_arr)

    print("  [2/13] Computing normal map...")
    normals = compute_normal_map(stitch_arr, mask, strength=2.0)

    print("  [3/13] Estimating thread directions...")
    thread_angle, anisotropy = estimate_thread_direction(stitch_arr, mask, block_size=12)

    print("  [4/13] Applying Blinn-Phong + anisotropic shading...")
    lit = apply_lighting(stitch_arr, normals, mask, thread_angle, anisotropy,
                         light_dir=light_dir,
                         ambient=0.55, diffuse_strength=0.35,
                         specular_strength=0.12, shininess=18.0,
                         aniso_strength=0.08, aniso_shininess=30.0)

    print("  [5/13] Computing ambient occlusion...")
    ao = compute_ambient_occlusion(stitch_arr, mask, radius=2.0, strength=0.12)
    # Apply AO: darken stitch valleys
    for c in range(3):
        lit[:, :, c] *= (1.0 - ao)
    lit = np.clip(lit, 0, 255)

    print("  [6/13] Adding per-thread micro-highlights...")
    lit = add_thread_microhighlights(lit, normals, mask, thread_angle, intensity=0.05)

    # --- Canvas setup ---
    canvas_w = w + padding * 2
    canvas_h = h + padding * 2

    print(f"  [7/13] Generating fabric texture ({canvas_w}x{canvas_h})...")
    fabric = generate_fabric_texture(canvas_w, canvas_h, color=fabric_color)
    canvas = fabric.astype(np.float64)

    # --- Pad mask to canvas size ---
    mask_padded = np.zeros((canvas_h, canvas_w), dtype=np.float64)
    mask_padded[padding:padding + h, padding:padding + w] = mask

    # --- Shadow ---
    print("  [8/13] Creating drop shadow...")
    shadow = create_patch_shadow(mask_padded, offset=(5, 6), blur_radius=10, opacity=0.50)
    for c in range(3):
        canvas[:, :, c] *= (1.0 - shadow * 0.8)

    # --- Composite lit patch onto canvas ---
    print("  [9/13] Compositing patch onto fabric...")
    for c in range(3):
        patch_channel = lit[:, :, c]
        canvas_region = canvas[padding:padding + h, padding:padding + w, c]
        canvas[padding:padding + h, padding:padding + w, c] = (
            canvas_region * (1 - mask) + patch_channel * mask
        )

    # --- Edge bevel (thickness illusion) ---
    print("  [10/13] Adding edge bevel for 3D thickness...")
    bevel = create_edge_bevel(mask_padded, bevel_width=5,
                               light_dir=(light_dir[0], light_dir[1]))
    # Apply bevel as brightness modulation
    bevel_intensity = 45.0  # How strong the bevel highlight/shadow is
    for c in range(3):
        canvas[:, :, c] += bevel * bevel_intensity
    canvas = np.clip(canvas, 0, 255)

    # --- Inner relief ---
    print("  [11/13] Adding inner relief at section boundaries...")
    inner_relief = create_inner_relief(stitch_arr, mask,
                                        light_dir=(light_dir[0], light_dir[1]),
                                        strength=0.07)
    relief_padded = np.zeros((canvas_h, canvas_w), dtype=np.float64)
    relief_padded[padding:padding + h, padding:padding + w] = inner_relief
    for c in range(3):
        canvas[:, :, c] *= (1.0 + relief_padded)
    canvas = np.clip(canvas, 0, 255)

    print("  [12/13] Adding merrow edge border...")
    merrow, merrow_band = create_merrow_edge(mask_padded, thickness=3)

    # Detect dominant edge color from the patch border pixels
    # Sample colors from the edge region of the lit patch
    edge_sample_mask = (mask > 0.3) & (mask < 0.95)
    if np.any(edge_sample_mask):
        edge_colors = lit[edge_sample_mask]
        avg_edge = np.mean(edge_colors, axis=0)
        # Make merrow edge slightly lighter than border
        edge_color = np.clip(avg_edge * 1.3 + 30, 0, 255)
    else:
        edge_color = np.array([160, 160, 165], dtype=np.float64)

    # Apply merrow edge with bump lighting for 3D thread appearance
    # Create mini-normal from merrow pattern for lit edge
    merrow_gx = ndimage.sobel(merrow, axis=1)
    merrow_gy = ndimage.sobel(merrow, axis=0)
    merrow_light = -(merrow_gx * light_dir[0] + merrow_gy * light_dir[1])
    merrow_light = merrow_light / (np.abs(merrow_light).max() + 1e-8) * 0.3

    for c in range(3):
        edge_val = edge_color[c] * (1.0 + merrow_light) * merrow
        canvas[:, :, c] = canvas[:, :, c] * (1 - merrow_band * 0.65) + edge_val * 0.65
    canvas = np.clip(canvas, 0, 255)

    # --- Photographic finishing ---
    print("  [13/13] Photographic finishing (DOF, color, vignette, grain)...")
    canvas = add_depth_of_field(canvas, mask_padded, max_blur=1.2)
    canvas = color_grade(canvas, warmth=0.02, contrast=1.06, saturation=1.12)
    canvas = add_vignette(canvas, strength=0.20, radius=0.60)
    canvas = add_film_grain(canvas, strength=3.0)

    # --- Save ---
    result = np.clip(canvas, 0, 255).astype(np.uint8)
    output_img = Image.fromarray(result, "RGB")
    output_img.save(str(output_path), quality=95)
    print(f"  Saved to {output_path}")

    return output_path


def save_debug_stages(input_path, output_dir):
    """Save intermediate stages for inspection."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stitch_img = Image.open(input_path)
    stitch_arr = np.array(stitch_img)[:, :, :3].astype(np.float64)
    h, w = stitch_arr.shape[:2]

    mask = extract_patch_mask(stitch_arr)
    normals = compute_normal_map(stitch_arr, mask, strength=2.0)
    thread_angle, anisotropy = estimate_thread_direction(stitch_arr, mask, block_size=12)

    # 1. Normal map visualization (standard purple/green/blue encoding)
    normal_vis = ((normals + 1.0) * 0.5 * 255).astype(np.uint8)
    Image.fromarray(normal_vis, "RGB").save(str(output_dir / "01_normal_map.png"))

    # 2. Mask
    mask_vis = (mask * 255).astype(np.uint8)
    Image.fromarray(mask_vis, "L").save(str(output_dir / "02_mask.png"))

    # 3. Thread direction + anisotropy
    thread_vis = np.zeros((h, w, 3), dtype=np.uint8)
    thread_vis[:, :, 0] = ((np.cos(thread_angle) + 1) * 0.5 * 255 * mask).astype(np.uint8)
    thread_vis[:, :, 1] = ((np.sin(thread_angle) + 1) * 0.5 * 255 * mask).astype(np.uint8)
    thread_vis[:, :, 2] = (anisotropy * 255 * mask).astype(np.uint8)
    Image.fromarray(thread_vis, "RGB").save(str(output_dir / "03_thread_direction.png"))

    # 4. Lit patch (after shading, before compositing)
    lit = apply_lighting(stitch_arr, normals, mask, thread_angle, anisotropy)
    lit = add_thread_microhighlights(lit, normals, mask, thread_angle)
    Image.fromarray(np.clip(lit, 0, 255).astype(np.uint8), "RGB").save(
        str(output_dir / "04_lit_patch.png"))

    # 5. Edge bevel visualization
    mask_padded = np.zeros((h + 100, w + 100), dtype=np.float64)
    mask_padded[50:50 + h, 50:50 + w] = mask
    bevel = create_edge_bevel(mask_padded, bevel_width=5)
    bevel_vis = ((bevel + 1) * 0.5 * 255).astype(np.uint8)
    Image.fromarray(bevel_vis, "L").save(str(output_dir / "05_edge_bevel.png"))

    # 6. Shadow
    shadow = create_patch_shadow(mask_padded)
    shadow_vis = (shadow * 255).astype(np.uint8)
    Image.fromarray(shadow_vis, "L").save(str(output_dir / "06_shadow.png"))

    print(f"Debug stages saved to {output_dir}/")


def create_comparison(input_path, output_path, comparison_path):
    """Create side-by-side comparison image."""
    original = Image.open(input_path)
    result = Image.open(output_path)

    # Make them the same height
    orig_w, orig_h = original.size
    res_w, res_h = result.size

    # Scale original to match result height
    scale = res_h / orig_h
    orig_scaled = original.resize((int(orig_w * scale), res_h), Image.LANCZOS)

    # Create comparison canvas
    gap = 20
    comp_w = orig_scaled.width + res_w + gap
    comp = Image.new("RGB", (comp_w, res_h + 40), (30, 30, 30))

    # Paste images
    comp.paste(orig_scaled, (0, 0))
    comp.paste(result, (orig_scaled.width + gap, 0))

    # Add labels
    draw = ImageDraw.Draw(comp)
    draw.text((orig_scaled.width // 2 - 30, res_h + 5), "BEFORE", fill=(180, 180, 180))
    draw.text((orig_scaled.width + gap + res_w // 2 - 20, res_h + 5), "AFTER", fill=(180, 180, 180))

    comp.save(str(comparison_path), quality=95)
    print(f"Comparison saved to {comparison_path}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.png> [output.png] [--debug] [--compare]")
        sys.exit(1)

    input_path = Path(sys.argv[1])

    # Find output path
    positional_args = [a for a in sys.argv[2:] if not a.startswith("--")]
    output_path = Path(positional_args[0]) if positional_args else \
        input_path.with_name(input_path.stem + "_photorealistic.png")

    debug = "--debug" in sys.argv
    compare = "--compare" in sys.argv

    if debug:
        save_debug_stages(input_path, output_path.parent / "debug")

    postprocess_photorealistic(input_path, output_path)

    if compare:
        comp_path = output_path.with_name(output_path.stem + "_comparison.png")
        create_comparison(input_path, output_path, comp_path)

    print("Done!")


if __name__ == "__main__":
    main()
