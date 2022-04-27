import pandas as pd
from astropy.io import fits
from yaml import parse
import numpy as np


def parse_header(filename, exclude=None, hdu="all"):
    pass


#     with fits.open(filename) as hdus:
#         headers = [hdu.header for hdu in hdus]


#     return reduce(lambda a,b: pd.merge())


def observation_table(filenames, **kwargs):
    rows = []
    for filename in filenames:
        rows.append(parse_header(filename, **kwargs))
    return pd.concat(rows, axis=0)


def parallactic_angle(header):
    if "D_IMRPAD" in header:
        return header["D_IMRPAD"] + header["LONPOLE"] - header["D_IMRPAP"]
    else:
        return parallactic_angle_altaz(header["ALTITUDE"], header["AZIMUTH"])


def parallactic_angle_hadec(ha, dec, lat=19.823806):
    """
    Calculate parallactic angle using the hour-angle and declination directly

    .. math::

        \\theta_\\mathrm{PA} = \\atan2{\\frac{\\sin\\theta_\\mathrm{HA}}{\\tan\\theta_\mathrm{lat}\\cos\\delta - \\sin\\delta \\cos\\theta_\\mathrm{HA}}}

    Parameters
    ----------
    ha : float
        hour-angle, in hour angles
    dec : float
        declination in degrees
    lat : float, optional
        latitude of observation in degrees, by default 19.823806

    Returns
    -------
    float
        parallactic angle, in degrees East of North
    """
    _ha = ha * np.pi / 12  # hour angle to radian
    _dec = np.deg2rad(dec)
    _lat = np.deg2rad(lat)
    sin_ha, cos_ha = np.sin(_ha), np.cos(_ha)
    sin_dec, cos_dec = np.sin(_dec), np.cos(_dec)
    pa = np.arctan2(sin_ha, np.tan(_lat) * cos_dec - sin_dec * cos_ha)
    return np.rad2deg(pa)


def parallactic_angle_altaz(alt, az, lat=19.823806):
    """
    Calculate parallactic angle using the altitude/elevation and aziumth directly

    ```math

    ```

    Parameters
    ----------
    alt : float
        altitude or elevation, in degrees
    az : float
        azimuth, in degrees CCW from North
    lat : float, optional
        latitude of observation in degrees, by default 19.823806

    Returns
    -------
    float
        parallactic angle, in degrees East of North
    """
    ## Astronomical Algorithms, Jean Meeus
    # get angles, rotate az to S
    _az = np.deg2rad(az) - np.pi
    _alt = np.deg2rad(alt)
    _lat = np.deg2rad(lat)
    # calculate values ahead of time
    sin_az, cos_az = np.sin(_az), np.cos(_az)
    sin_alt, cos_alt = np.sin(_alt), np.cos(_alt)
    sin_lat, cos_lat = np.sin(_lat), np.cos(_lat)
    # get declination
    dec = np.arcsin(sin_alt * sin_lat - cos_alt * cos_lat * cos_az)
    # get hour angle
    ha = np.arctan2(sin_az, cos_az * sin_lat + np.tan(_alt) * cos_lat)
    # get parallactic angle
    pa = np.arctan2(np.sin(ha), np.tan(_lat) * np.cos(dec) - np.sin(dec) * np.cos(ha))
    return np.rad2deg(pa)
