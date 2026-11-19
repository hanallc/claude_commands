"""Select page settings and share installed configuration reads."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import TypedDict, cast, override
import json
import os
import pwd

from scripts.lib import conf

# These stems use the tree under test, except for the listed installed keys.
PAGE_SETTINGS = {
    "console": ("for_you.host",),
    "disks": (),
    "memory_feed": ("output_folder",),
    "production": ("mac_run_log", "mac_run_launch", "mac_run_status", "mac_run_bundle",
                   "mac_run_example_log_prefix", "mac_run_source"),
    "review_links": ("server_port", "review_attachments_root"),
}


def installed_checkout() -> Path:
    checkout = Path.home() / ".claude"
    if not (checkout / "scripts/lib/conf.py").is_file():
        checkout = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude"
    return checkout.resolve()


class UnreadableSetting(TypedDict):
    reason: str


@dataclass(frozen=True)
class InstalledSettings:
    directory: Path
    repository: Path
    files: dict[str, str | UnreadableSetting]

    @classmethod
    def read(cls) -> InstalledSettings:
        directory = conf.directory().absolute()
        files: dict[str, str | UnreadableSetting] = {}
        for path in directory.glob("*.conf"):
            try:
                files[path.name] = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except (OSError, UnicodeError) as error:
                files[path.name] = {"reason": str(error)}
        return cls(directory, installed_checkout() / "config", files)

    def save(self, path: Path) -> None:
        _ = path.write_text(json.dumps([str(self.directory), str(self.repository), self.files]), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> InstalledSettings:
        directory, repository, files = cast(tuple[str, str, dict[str, str | UnreadableSetting]],
                                            json.loads(path.read_text(encoding="utf-8")))
        return cls(Path(directory), Path(repository), files)

    def select(self, tree: Path) -> Callable[[str], conf.ConfigRead]:
        """Use the scanned parser with one source for installed recovery."""
        # The exported base uses the same configuration reader interface.
        from scripts.lib.conf import _MissingKey, _UnreadableFile, _converted, _parse, _setting  # pyright: ignore[reportPrivateUsage]

        read_path = cast(Callable[..., conf.ConfigRead], conf.read)
        custom_values = hasattr(conf.ConfigRead, "value")
        repository_allowed = "CONF_DIR" not in os.environ

        def repository_path(stem: str) -> Path:
            return self.repository / f"{stem}.conf"

        @cache
        def installed(file: str | Path) -> conf.ConfigRead:
            source = file.expanduser() if isinstance(file, Path) else self.directory / f"{file}.conf"
            name = source.name
            data = self.files.get(name)
            if isinstance(data, str):
                settings = _parse(data)
            elif data is None:
                settings = _UnreadableFile(str(FileNotFoundError(2, "No such file or directory", str(source))), True)
            else:
                settings = _UnreadableFile(data["reason"], False)
            return conf.ConfigRead(file, source, settings, repository_allowed)

        def installed_value(key: str, keys: tuple[str, ...]) -> bool:
            return any(key == prefix or key.startswith(prefix + ".") for prefix in keys)

        @dataclass(frozen=True, kw_only=True)
        class PageSettings(conf.ConfigRead):
            page: conf.ConfigRead
            shared: conf.ConfigRead
            shared_keys: tuple[str, ...]

            @override
            def _resolve[Value](self, key: str, convert: Callable[[str], Value], *, warn: bool,
                                recover_invalid: bool = True, allow_blank: bool = False,
                                default: Value | conf.UseConfiguredValue = conf.UseConfiguredValue.CURRENT
                                ) -> conf.UsableLiveValue[Value] | conf.RecoveredValue[Value] | conf.UnavailableValue:
                if installed_value(key, self.shared_keys):
                    if custom_values:
                        resolve = cast(Callable[..., conf.UsableLiveValue[Value] | conf.RecoveredValue[Value]
                                               | conf.UnavailableValue], self.shared._resolve)
                        return resolve(key, convert, warn=warn, recover_invalid=recover_invalid,
                                       allow_blank=allow_blank, default=default)
                    return self.shared._resolve(key, convert, warn=warn, recover_invalid=recover_invalid)
                value = "<missing>"
                try:
                    if not isinstance(self.page.settings, dict):
                        raise ValueError(self.page.settings.reason)
                    setting = _setting(self.page.settings, key)
                    value = setting.value
                    if allow_blank and not value and not setting.limit.startswith(("# whole:", "# range:", "# one of:")):
                        return conf.UsableLiveValue(convert(""))
                    return conf.UsableLiveValue(_converted(setting, convert))
                except (OSError, UnicodeError, ValueError, ArithmeticError) as error:
                    if isinstance(error, _MissingKey) and not isinstance(default, conf.UseConfiguredValue):
                        return conf.UsableLiveValue(default)
                    return conf.UnavailableValue(f"{self.page.source}: {key}={value!r}: {error}")

        @cache
        def selected(file: str | Path) -> conf.ConfigRead:
            shared = installed(file)
            stem = shared.source.stem
            keys = PAGE_SETTINGS.get(stem)
            page_source = tree / "config" / f"{stem}.conf"
            if keys is None or (stem not in ("console", "review_links") and not page_source.is_file()):
                return shared
            page = read_path(page_source)
            if not isinstance(page.settings, dict):
                return replace(page, file=file, repository_allowed=False)
            settings = {key: value for key, value in page.settings.items() if not installed_value(key, keys)}
            if isinstance(shared.settings, dict):
                settings.update({key: value for key, value in shared.settings.items() if installed_value(key, keys)})
            return PageSettings(file, page.source, settings, False, page=page, shared=shared, shared_keys=keys)

        def read(file: str | Path, **options: object) -> conf.ConfigRead:
            source = conf.path(file)
            if not options and source.parent == self.directory and source.suffix == ".conf":
                return selected(source if isinstance(file, Path) else file)
            return read_path(file, **options)

        def read_or_repository(stem: str) -> conf.ConfigRead:
            if (not repository_allowed and stem not in PAGE_SETTINGS and f"{stem}.conf" not in self.files):
                return read_path(repository_path(stem))
            return read(stem)

        from scripts.lib import conf_reads

        read_text = conf_reads.read_text

        def selected_text(path: str | os.PathLike[str]) -> str:
            source = Path(path)
            if source.parent != self.directory or source.suffix != ".conf":
                return read_text(path)
            data = self.files.get(source.name)
            if isinstance(data, str):
                return data
            if data is None:
                raise FileNotFoundError(2, "No such file or directory", source)
            raise OSError(data["reason"])

        if not hasattr(conf.ConfigRead, "source_missing"):
            from scripts.production import change_routing

            read_routing = change_routing.routing_setting

            def routing_setting(file: str, key: str, *, sources: list[Path] | None = None) -> str:
                if sources is not None and f"{file}.conf" not in self.files:
                    settings = read(file)
                    sources.append(settings.source)
                    return settings.text(key)
                return read_routing(file, key, sources=sources)

            change_routing.routing_setting = routing_setting

        conf.directory = lambda: self.directory
        conf_reads.read_text = selected_text
        conf.read = read
        conf.read_or_repository = read_or_repository
        setattr(conf, "_repository_path", repository_path)
        return installed
