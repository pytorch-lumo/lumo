import json
import os

__all__ = ['glob', 'global_config_path', 'local_config_path']

GLOBAL_DEFAULT = {
    'home': os.path.expanduser("~/.lumo/"),
    'cache_dir': os.path.expanduser("~/.cache/lumo"),
}


def global_config_path():
    return os.path.expanduser("~/.lumorc.json")


def local_config_path():
    from lumo.utils.repository import git_dir
    res = git_dir()
    if res:
        return os.path.join(res, ".lumorc.json")
    return None


def get_config(path, default):
    if path is None:
        return default

    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as r:
                config = json.load(r)
            return config
        except Exception as e:
            print(f'Error read {path}')

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as w:
        json.dump(default, w)
    return default


def get_runtime_config():
    glob_cfg = get_config(global_config_path(), GLOBAL_DEFAULT)
    local_cfg = get_config(local_config_path(), {})
    cfg = GLOBAL_DEFAULT
    cfg.update(glob_cfg)
    cfg.update(local_cfg)
    return cfg


# A dict object contains runtime configuration.
glob = get_runtime_config()
