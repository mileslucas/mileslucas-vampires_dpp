import numpy as np
import sep
from astropy.io import fits

from vampires_dpp.image_processing import radial_profile_image, shift_frame
from vampires_dpp.indexing import (
    cutout_slice,
    frame_center,
    frame_radii,
    lamd_to_pixel,
    window_slices,
)
from vampires_dpp.psf_models import fit_model
from vampires_dpp.util import get_paths


def safe_aperture_sum(frame, r, center=None, ann_rad=None):
    if center is None:
        center = frame_center(frame)
    mask = ~np.isfinite(frame)
    flux, fluxerr, flag = sep.sum_circle(
        np.ascontiguousarray(frame).astype("f4"),
        (center[1],),
        (center[0],),
        r,
        mask=mask,
        bkgann=ann_rad,
    )

    return flux[0]


def analyze_frame(
    frame,
    aper_rad,
    header=None,
    ann_rad=None,
    model="gaussian",
    recenter=True,
    header_key="",
    **kwargs,
):
    ## fit PSF to center
    inds = cutout_slice(frame, **kwargs)
    model_fit = fit_model(frame, inds, model)

    old_ctr = frame_center(frame)
    ctr = np.array((model_fit["y"], model_fit["x"]))
    if any(np.abs(old_ctr - ctr) > 10):
        ctr = old_ctr

    phot = safe_aperture_sum(frame, r=aper_rad, center=ctr, ann_rad=ann_rad)
    ## use PSF centers to recenter
    if recenter:
        offsets = frame_center(frame) - ctr
        frame = shift_frame(frame, offsets)

    # update header
    if header is not None:
        header[f"MODEL"] = model, "PSF model name"
        header[f"MOD_AMP{header_key:1s}"] = model_fit["amplitude"], "[adu] PSF model amplitude"
        header[f"MOD_X{header_key:1s}"] = model_fit["x"], "[px] PSF model x"
        header[f"MOD_Y{header_key:1s}"] = model_fit["y"], "[px] PSF model y"
        header[f"FLUX{header_key:1s}"] = phot, "[adu] Aperture photometry flux"
        header[f"PHOTRAD{header_key:1s}"] = aper_rad, "[px] Aperture photometry radius"
        header[f"MEDFLUX{header_key:1s}"] = np.nanmedian(frame), "[adu] Median frame flux"
        header[f"SUMFLUX{header_key:1s}"] = np.nansum(frame), "[adu] Total frame flux"
        header[f"PEAKFLX{header_key:1s}"] = np.nanmax(frame), "[adu] Peak frame flux"

    return frame, header


def analyze_satspots_frame(
    frame,
    aper_rad,
    subtract_radprof=True,
    header=None,
    ann_rad=None,
    model="gaussian",
    recenter=True,
    header_key="",
    **kwargs,
):
    ## subtract radial profile
    data = frame
    if subtract_radprof:
        profile = radial_profile_image(frame)
        data = frame - profile
    ## fit PSF to each satellite spot
    slices = window_slices(frame, **kwargs)
    N = len(slices)
    ave_x = ave_y = ave_amp = ave_flux = 0
    for sl in slices:
        model_fit = fit_model(data, sl, model)
        ave_x += model_fit["x"] / N
        ave_y += model_fit["y"] / N
        ave_amp += model_fit["amplitude"] / N
        phot = safe_aperture_sum(
            data, r=aper_rad, center=(model_fit["y"], model_fit["x"]), ann_rad=ann_rad
        )
        ave_flux += phot / N

    old_ctr = frame_center(frame)
    ctr = np.array((ave_y, ave_x))
    if any(np.abs(old_ctr - ctr) > 10):
        ctr = old_ctr
        ave_flux = 0

    ## use PSF centers to recenter
    if recenter:
        offsets = np.array(frame_center(frame)) - np.array(ctr)
        frame = shift_frame(frame, offsets)

    # update header
    if header is not None:
        header[f"MODEL"] = model, "PSF model name"
        header[f"MOD_AMP{header_key:1s}"] = ave_amp, "[adu] PSF model amplitude"
        header[f"MOD_X{header_key:1s}"] = ave_x, "[px] PSF model x"
        header[f"MOD_Y{header_key:1s}"] = ave_y, "[px] PSF model y"
        header[f"PHOTFLX{header_key:1s}"] = ave_flux, "[adu] Aperture photometry flux"
        header[f"PHOTRAD{header_key:1s}"] = aper_rad, "[px] Aperture photometry radius"
        header[f"MEDFLUX{header_key:1s}"] = np.nanmedian(frame), "[adu] Median frame flux"
        header[f"SUMFLUX{header_key:1s}"] = np.nansum(frame), "[adu] Total frame flux"
        header[f"PEAKFLX{header_key:1s}"] = np.nanmax(frame), "[adu] Peak frame flux"
    return frame, header


def analyze_file(
    frame, header, filename, aper_rad, radius=None, coronagraphic=False, force=False, **kwargs
):
    path, outpath = get_paths(filename, suffix="analyzed", **kwargs)
    if not force and outpath.is_file() and path.stat().st_mtime < outpath.stat().st_mtime:
        frame, header = fits.getdata(path, header=True)
        return frame, header
    if "MBI" in header["OBS-MOD"].upper():
        for i, k in zip(range(frame.shape[0]), ("A", "B", "C", "D")):
            if coronagraphic:
                frame[i], header = analyze_satspots_frame(
                    frame[i],
                    aper_rad,
                    header=header,
                    header_key=k,
                    radius=lamd_to_pixel(radius, header["FILTER01"]),
                    **kwargs,
                )
            else:
                frame[i], header = analyze_frame(
                    frame[i], aper_rad, header=header, header_key=k, **kwargs
                )
    else:
        if coronagraphic:
            frame, header = analyze_satspots_frame(
                frame,
                aper_rad,
                header=header,
                radius=lamd_to_pixel(radius, header["FILTER01"]),
                **kwargs,
            )
        else:
            frame, header = analyze_frame(frame, aper_rad, header=header, **kwargs)

    return frame, header
