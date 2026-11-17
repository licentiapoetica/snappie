# Uploader plugins

Snappie uploads with *uploaders* you set up in Settings → Uploads. Each one has a
type: ShareX custom uploader (`.sxcu`), SFTP, Command, or a type added by a
plugin. Write a plugin when a service needs more than one HTTP request, a
login, or an API that `.sxcu` files can't describe.

Try the **Command** type first: it runs any program (rclone, s3cmd, curl, a
shell script) and needs no Python.

## Where plugins go

A plugin is a Python file in `~/.local/share/snappie/plugins/`. The Add menu
has **Open Plugins Folder**. Snappie loads the files again when they change,
so you don't have to restart it. A plugin that fails to load shows up in the
Add menu with its error. The full traceback is in
`~/.local/state/snappie/snappie.log`.

## A plugin

```python
from snappie.uploaders import Field, Plugin, UploadError, post_file


class Litterbox(Plugin):
    id = "litterbox"                       # stored in uploader files: never change it
    name = "Litterbox"                     # in the Add menu
    description = "Temporary uploads to litterbox.catbox.moe."
    icon = "cloud-upload"                  # an icon theme name
    fields = [
        Field("time", "Keep for", "choice",
              choices=[("1h", "1 hour"), ("12h", "12 hours"), ("24h", "1 day"), ("72h", "3 days")]),
    ]

    def summary(self, config):
        return f"Kept for {config['time']}"

    def upload(self, path, config, progress, cancel):
        resp = post_file("https://litterbox.catbox.moe/resources/internals/api.php", path,
                         field="fileToUpload", fields={"reqtype": "fileupload", "time": config["time"]},
                         progress=progress, cancel=cancel)
        url = resp.text.strip()
        if not url.startswith("https://"):
            raise UploadError(f"Litterbox said: {url[:200]}")
        return url
```

Save this as `litterbox.py` in the plugins folder. **Add → Litterbox…** then
shows a form with a name and a "Keep for" choice.

## The Plugin class

| attribute / method | what |
|---|---|
| `id` | Lowercase and unique. Uploader files store it, so keep it the same. Built-in ids (`sxcu`, `sftp`, `command`) are taken. |
| `name`, `description`, `icon` | Shown in the Add menu and at the top of the form. |
| `fields` | A list of `Field`s: the form, and the keys of `config`. |
| `upload(path, config, progress, cancel)` | Does the upload and returns the link, or an `UploadResult`. |
| `validate(config)` | Raise `UploadError` for a config that can't work. Called when the form is saved. The default checks required fields; call `super().validate(config)` if you override it. |
| `summary(config)` | A line or two shown under the uploader list. |
| `available()` | Return a message when the plugin can't work here, e.g. a missing module. `""` means it can. |

`upload()` runs on a worker thread. It must not touch Qt widgets. One plugin
instance serves every uploader of its type, so keep per-upload state in local
variables.

- `config` holds this uploader's values. Fields added in a newer version of
  your plugin get their defaults.
- `progress(sent, total)` moves the progress bar. Call it as bytes go out.
- `cancel` is a `threading.Event`. When it's set, stop and raise `Cancelled`.
- Raise `UploadError("short message")` when something fails. The message
  appears in the notification. Any other exception is caught and reported the
  same way, with its type.

Return `UploadResult(url, delete_url="", thumbnail_url="")` to give a deletion
link too. The history keeps it.

## Fields

`Field(key, label, kind="text", default=None, required=False, help="",
placeholder="", choices=(), minimum=0, maximum=…, special="")`

| kind | widget | value |
|---|---|---|
| `text` | line edit | `str` (trimmed) |
| `password` | hidden line edit with a Show button | `str` |
| `int` | spin box from `minimum` to `maximum`; `special` is shown for the minimum, e.g. "Default" | `int` |
| `bool` | check box | `bool` |
| `choice` | drop-down list of `choices`: `[(value, label), …]` | the chosen value |
| `file` | line edit with Browse… | `str` (may start with `~`) |

`help` is a hint under the field. The uploader file is readable only by you,
because passwords and tokens are kept in it.

## Helpers for HTTP

All of these are in `snappie.uploaders`:

- `post_file(url, path, field="file", fields=None, headers=None, method="POST",
  progress=None, cancel=None)` sends the file as `multipart/form-data`, the way
  a browser form does. It streams from disk with progress and cancel, and
  returns the `requests.Response`. HTTP errors raise `UploadError`.
- `request(method, url, **kw)` is `requests.request` with Snappie's timeouts,
  User-Agent and readable errors. Servers with a self-signed certificate that
  the user trusted work too.
- `StreamBody([bytes or path, …], progress, cancel)` is a request body read in
  chunks, for APIs that want the raw file (`data=StreamBody([path], …)`).
- `http_error(response, message="")` makes an `UploadError` with the response
  attached. **Test Upload → Show Response…** can then show it.

A plugin may also use any other Python module, such as `boto3` for S3. Check
for it in `available()`, so users see what to install.

## Uploader files

Uploaders live in `~/.config/snappie/uploaders/`. A plugin uploader is a JSON
file:

```json
{"Plugin": "litterbox", "Name": "Litterbox (1 day)", "Config": {"time": "24h"}}
```

`snappie upload -u "Litterbox (1 day)" FILE` uses it by name. If its plugin is
missing, the uploader stays in the list and says why it can't be used.
