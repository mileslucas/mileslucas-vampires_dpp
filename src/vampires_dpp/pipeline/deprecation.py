from packaging.version import Version

from vampires_dpp import __version__
from vampires_dpp.pipeline.config import *
from vampires_dpp.pipeline.pipeline import Pipeline


def upgrade_config(config_dict: dict) -> Pipeline:
    f"""
    Tries to upgrade an old configuration to a new version, appropriately reflecting name changes and prompting the user for input when needed.

    .. admonition::
        :class: tip

        In some cases (e.g. pre v0.6) it is necessary to recreate a configuration using `dpp create` because this function is not able to convert those version ranges.

    Parameters
    ----------
    config : Pipeline
        Input Pipeline configuration

    Returns
    -------
    Pipeline
        Pipeline configuration upgraded to the current package version ({__version__}).
    """
    config_version = Version(config_dict["version"])
    ## start with version 0.7, first breaking changes
    if config_version < Version("0.7"):
        config_dict = upgrade_to_0p7(config_dict)

    pipeline = Pipeline(**config_dict)
    return pipeline


def upgrade_to_0p7(config_dict):
    if "polarimetry" in config_dict:
        # added method to polarimetry between "difference" and "mueller"
        # defaults to difference since that was all that was supported
        config_dict["polarimetry"]["method"] = "difference"
        # derotate_pa renamed to adi_sync and logic inverted
        adi_sync = not config_dict["polarimetry"].get("derotate_pa", False)
        config_dict["polarimetry"]["adi_sync"] = adi_sync
        del config_dict["polarimetry"]["derotate_pa"]

    return config_dict
