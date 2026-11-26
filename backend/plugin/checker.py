import io
import zipfile

from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import rtoml

from dulwich import porcelain

from backend.common.dataclasses import PluginCheckItem, PluginCheckReport
from backend.common.exception import errors
from backend.plugin.compatibility import check_fba_compatibility
from backend.plugin.conflict import get_menu_conflicts, get_route_conflicts, get_table_conflicts
from backend.plugin.core import check_plugin_installed
from backend.plugin.errors import PluginConfigError
from backend.plugin.installer import parse_plugin_name
from backend.plugin.sql import REQUIRED_SQL_FILES
from backend.plugin.validator import validate_plugin_config
from backend.utils.pattern_validate import is_git_url


def _extract_zip_plugin(path: str, work_dir: Path) -> tuple[str, Path]:
    """
    解压 ZIP 插件到临时目录

    :param path: ZIP 插件的本地完整路径
    :param work_dir: 临时工作目录
    :return:
    """
    plugin_name = parse_plugin_name(Path(path).name.split('.')[0].strip())

    with zipfile.ZipFile(path) as zf:
        plugin_namelist = zf.namelist()
        if not plugin_namelist:
            raise errors.RequestError(msg='插件压缩包内容非法')
        plugin_dir_name = plugin_namelist[0].split('/', 1)[0].strip()
        if not plugin_dir_name:
            raise errors.RequestError(msg='插件压缩包内容非法')

        prefix = f'{plugin_dir_name}/'
        members = []
        for member in zf.infolist():
            if not member.filename.startswith(prefix):
                continue
            relative_filename = member.filename.removeprefix(prefix)
            if not relative_filename or member.is_dir():
                continue
            member.filename = relative_filename
            members.append(member)
        if not members:
            raise errors.RequestError(msg='插件压缩包内容非法')

        destination = work_dir / plugin_dir_name
        destination.mkdir(parents=True, exist_ok=True)
        zf.extractall(destination, members)

    return plugin_name, destination


def _clone_git_plugin(repo_url: str, work_dir: Path) -> tuple[str, Path]:
    """
    克隆 Git 插件到临时目录

    :param repo_url: Git 插件的仓库地址
    :param work_dir: 临时工作目录
    :return:
    """
    match = is_git_url(repo_url)
    if not match:
        raise errors.RequestError(msg='Git 仓库地址格式非法，仅支持 HTTP/HTTPS 协议')

    repo_name = match.group('repo')
    destination = work_dir / repo_name
    errstream = io.BytesIO()
    try:
        repo = porcelain.clone(repo_url, destination, checkout=True, errstream=errstream)
    except Exception as e:
        detail = errstream.getvalue().decode('utf-8', errors='replace').strip()
        msg = f'Git 插件仓库克隆失败：{e!s}'
        if detail:
            msg += f'，{detail}'
        raise errors.RequestError(msg=msg) from e
    repo.close()

    return repo_name, destination


def _notify(progress: Callable[[str], None] | None, message: str) -> None:
    """
    输出检测进度

    :param progress: 进度回调
    :param message: 进度消息
    :return:
    """
    if progress:
        progress(message)


def _check_manifest(plugin_name: str, plugin_dir: Path, report: PluginCheckReport) -> dict[str, Any] | None:
    """
    检测插件清单

    :param plugin_name: 插件名称
    :param plugin_dir: 插件目录
    :param report: 检测报告
    :return: 解析后的插件清单，校验不通过时返回 None
    """
    manifest_path = plugin_dir / 'plugin.toml'
    if not manifest_path.exists():
        report.items.append(PluginCheckItem(name='插件清单', passed=False, detail='缺少 plugin.toml 配置文件'))
        return None

    try:
        manifest = rtoml.loads(manifest_path.read_text(encoding='utf-8'))
    except Exception as e:
        report.items.append(PluginCheckItem(name='插件清单', passed=False, detail=f'配置文件格式非法：{e!s}'))
        return None
    report.items.append(PluginCheckItem(name='插件清单', passed=True, detail='配置文件解析成功'))

    plugin_info = manifest.get('plugin')
    specifier = plugin_info.get('fba_version') if isinstance(plugin_info, dict) else None
    if not specifier:
        report.items.append(PluginCheckItem(name='fba 版本兼容性', passed=False, detail='未声明 fba_version 版本区间'))
    else:
        try:
            result = check_fba_compatibility(plugin_name, specifier)
        except PluginConfigError as e:
            report.items.append(PluginCheckItem(name='fba 版本兼容性', passed=False, detail=str(e)))
        else:
            report.items.append(
                PluginCheckItem(
                    name='fba 版本兼容性',
                    passed=result.compatible,
                    detail=f'要求 fba 版本 {result.required}，当前版本 {result.current}',
                )
            )

    try:
        plugin_level = validate_plugin_config(plugin_name, manifest)
    except PluginConfigError as e:
        report.items.append(PluginCheckItem(name='清单字段校验', passed=False, detail=str(e)))
    else:
        report.items.append(PluginCheckItem(name='清单字段校验', passed=True, detail=f'插件类型 {plugin_level.value}'))

    return manifest


def _check_install_condition(
    plugin_name: str, plugin_dir: Path, manifest: dict[str, Any], report: PluginCheckReport
) -> None:
    """
    检测插件安装条件

    :param plugin_name: 插件名称
    :param plugin_dir: 插件目录
    :param manifest: 插件清单
    :param report: 检测报告
    :return:
    """
    required_files = [name for name in ('README.md', '__init__.py') if not (plugin_dir / name).is_file()]
    if required_files:
        report.items.append(
            PluginCheckItem(name='插件结构', passed=False, detail=f'缺少必要文件：{", ".join(required_files)}')
        )
    else:
        report.items.append(PluginCheckItem(name='插件结构', passed=True, detail='必要文件齐全'))

    if check_plugin_installed(plugin_name):
        report.items.append(PluginCheckItem(name='安装状态', passed=False, detail='插件已安装'))
    else:
        report.items.append(PluginCheckItem(name='安装状态', passed=True, detail='插件未安装'))

    plugin_info = manifest.get('plugin')
    depends_on = plugin_info.get('depends_on', []) if isinstance(plugin_info, dict) else []
    missing_depends = [name for name in depends_on if not check_plugin_installed(name)]
    if missing_depends:
        report.items.append(
            PluginCheckItem(name='插件依赖', passed=False, detail=f'依赖插件未安装：{", ".join(missing_depends)}')
        )
    elif depends_on:
        report.items.append(
            PluginCheckItem(name='插件依赖', passed=True, detail=f'依赖插件均已安装：{", ".join(depends_on)}')
        )
    else:
        report.items.append(PluginCheckItem(name='插件依赖', passed=True, detail='无插件依赖'))

    has_model = (plugin_dir / 'model').is_dir()
    has_sql_dir = (plugin_dir / 'sql').is_dir()
    if not has_model and not has_sql_dir:
        report.items.append(PluginCheckItem(name='SQL 脚本', passed=True, detail='插件无数据表，无需 SQL 脚本'))
    else:
        sql_dir = plugin_dir / 'sql' / 'postgresql'
        missing_sql = [name for name in REQUIRED_SQL_FILES if not (sql_dir / name).is_file()]
        if missing_sql:
            report.items.append(
                PluginCheckItem(name='SQL 脚本', passed=False, detail=f'缺少 SQL 脚本：{", ".join(missing_sql)}')
            )
        else:
            report.items.append(PluginCheckItem(name='SQL 脚本', passed=True, detail='SQL 脚本完整'))

    if (plugin_dir / '.env.example').is_file():
        report.items.append(PluginCheckItem(name='环境变量', passed=True, detail='安装时将追加到后端 .env 文件'))
    else:
        report.items.append(PluginCheckItem(name='环境变量', passed=True, detail='无环境变量文件'))

    if (plugin_dir / 'requirements.txt').is_file():
        report.items.append(PluginCheckItem(name='依赖包', passed=True, detail='安装时将自动安装依赖'))
    else:
        report.items.append(PluginCheckItem(name='依赖包', passed=True, detail='无依赖包文件'))


def _check_conflict(
    plugin_dir: Path,
    manifest: dict[str, Any],
    report: PluginCheckReport,
    progress: Callable[[str], None] | None = None,
) -> None:
    """
    检测插件数据与接口冲突

    :param plugin_dir: 插件目录
    :param manifest: 插件清单
    :param report: 检测报告
    :param progress: 进度回调
    :return:
    """
    _notify(progress, '正在检测数据表冲突...')
    table_conflicts = get_table_conflicts(plugin_dir)
    report.items.append(
        PluginCheckItem(
            name='数据表冲突',
            passed=not table_conflicts,
            detail='无数据表冲突' if not table_conflicts else f'与现有数据表冲突：{", ".join(table_conflicts)}',
        )
    )

    _notify(progress, '正在检测菜单权限冲突...')
    menu_conflicts = get_menu_conflicts(plugin_dir)
    report.items.append(
        PluginCheckItem(
            name='菜单权限冲突',
            passed=not menu_conflicts,
            detail='无菜单权限冲突' if not menu_conflicts else f'与现有菜单权限冲突：{", ".join(menu_conflicts)}',
        )
    )

    _notify(progress, '正在检测接口冲突...')
    route_conflicts = get_route_conflicts(plugin_dir, manifest)
    report.items.append(
        PluginCheckItem(
            name='接口冲突',
            passed=not route_conflicts,
            detail='无接口冲突' if not route_conflicts else f'与现有接口冲突：{", ".join(route_conflicts)}',
        )
    )


def check_plugin_installation(
    *,
    path: str | None = None,
    repo_url: str | None = None,
    progress: Callable[[str], None] | None = None,
) -> PluginCheckReport:
    """
    检测插件安装条件

    仅解析插件文件并校验安装前置条件，不安装插件、不执行插件代码、不修改项目文件

    :param path: ZIP 插件的本地完整路径
    :param repo_url: Git 插件的仓库地址
    :param progress: 进度回调，用于即时反馈检测进度
    :return:
    """
    with TemporaryDirectory() as work_dir:
        work_path = Path(work_dir)
        if path is not None:
            if repo_url is not None:
                raise errors.RequestError(msg='path 和 repo_url 不能同时指定')
            _notify(progress, '正在解压插件压缩包...')
            plugin_name, plugin_dir = _extract_zip_plugin(path, work_path)
        elif repo_url is not None:
            _notify(progress, '正在克隆 Git 仓库...')
            plugin_name, plugin_dir = _clone_git_plugin(repo_url, work_path)
        else:
            raise errors.RequestError(msg='path 或 repo_url 必须指定其中一项')

        report = PluginCheckReport(plugin_name=plugin_name)
        _notify(progress, '正在解析插件清单...')
        manifest = _check_manifest(plugin_name, plugin_dir, report)
        if manifest is not None:
            _notify(progress, '正在检测插件结构与依赖...')
            _check_install_condition(plugin_name, plugin_dir, manifest, report)
            _check_conflict(plugin_dir, manifest, report, progress)

    return report
