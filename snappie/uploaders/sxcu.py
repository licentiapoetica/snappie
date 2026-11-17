"""ShareX custom uploaders: the .sxcu engine in snappie/upload.py as an uploader type."""

from .. import upload as http
from . import Plugin


class Sxcu(Plugin):
    id = "sxcu"
    name = "ShareX custom uploader"
    description = "An HTTP upload described by a ShareX .sxcu file."
    icon = "network-server"
    editable = False    # imported, not set up with the form

    def summary(self, config):
        try:
            cfg = http.load_config(config)
        except http.UploadError as e:
            return str(e)
        dest = cfg.get("DestinationType") or "any"
        return f"{cfg['RequestMethod']} {cfg.get('RequestURL', '')}\nBody: {cfg['Body']}  ·  For: {dest}"

    def upload(self, path, config, progress, cancel):
        return http.upload(path, config, progress=progress, cancel=cancel)
