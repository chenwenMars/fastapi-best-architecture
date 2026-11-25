import ast
import re

from pathlib import Path
from typing import Any, Final

import rtoml

from backend.core.conf import settings
from backend.core.path_conf import BASE_PATH, PLUGIN_DIR, POSTGRESQL_SCRIPT_DIR
from backend.plugin.core import get_plugins

# sys_menu 插入语句匹配模式
_MENU_INSERT_PATTERN: Final = re.compile(
    r'insert\s+into\s+sys_menu\s*\(([^)]*)\)\s*values\s*(.*?);', re.IGNORECASE | re.DOTALL
)

# 插件模型目录名
_MODEL_DIR: Final = 'model'


def _parse_module(file_path: Path) -> ast.Module | None:
    """
    解析 Python 模块

    :param file_path: 文件路径
    :return:
    """
    try:
        return ast.parse(file_path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, ValueError):
        return None


def _resolve_prefix(node: ast.AST) -> str:
    """
    解析路由前缀

    :param node: 表达式节点
    :return:
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'settings':
        return str(getattr(settings, node.attr, ''))
    if isinstance(node, ast.JoinedStr):
        return ''.join(_resolve_prefix(value) for value in node.values)
    if isinstance(node, ast.FormattedValue):
        return _resolve_prefix(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return f'{_resolve_prefix(node.left)}{_resolve_prefix(node.right)}'
    return ''


def _collect_api_routers(tree: ast.Module) -> dict[str, str]:
    """
    提取模块中的 APIRouter 变量与前缀

    :param tree: 模块语法树
    :return:
    """
    routers: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if not isinstance(node.value.func, ast.Name) or node.value.func.id != 'APIRouter':
            continue
        prefix = ''
        for keyword in node.value.keywords:
            if keyword.arg == 'prefix':
                prefix = _resolve_prefix(keyword.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                routers[target.id] = prefix
    return routers


def _collect_routes(tree: ast.Module, router_name: str = 'router') -> list[tuple[str, str]]:
    """
    提取路由方法与路径

    :param tree: 模块语法树
    :param router_name: 路由变量名
    :return:
    """
    routes: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != router_name:
                continue
            path = ''
            if decorator.args:
                path = _resolve_prefix(decorator.args[0])
            routes.append((decorator.func.attr.upper(), path))
    return routes


def _collect_includes(tree: ast.Module) -> dict[str, str]:
    """
    提取 include_router 调用的路由别名与前缀

    :param tree: 模块语法树
    :return:
    """
    includes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != 'include_router' or not node.args or not isinstance(node.args[0], ast.Name):
            continue
        prefix = ''
        for keyword in node.keywords:
            if keyword.arg == 'prefix':
                prefix = _resolve_prefix(keyword.value)
        includes[node.args[0].id] = prefix
    return includes


def _collect_router_imports(tree: ast.Module) -> dict[str, str]:
    """
    提取 router 的导入别名与模块路径

    :param tree: 模块语法树
    :return:
    """
    imports: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        for alias in node.names:
            if alias.name == 'router':
                imports[alias.asname or 'router'] = node.module
    return imports


def _import_module_prefix(module_path: str) -> str:
    """
    获取目标模块路由的前缀

    目标模块属于核心应用，延迟导入避免与插件模块产生循环依赖

    :param module_path: 模块路径
    :return:
    """
    from importlib import import_module

    try:
        module = import_module(module_path)
    except Exception:
        return ''

    router = getattr(module, 'router', None)
    return str(getattr(router, 'prefix', '') or '')


def _get_app_prefix(extend: str) -> str:
    """
    获取目标应用的接口前缀

    :param extend: 目标应用目录名称
    :return:
    """
    router_file = BASE_PATH / 'app' / extend / 'api' / 'router.py'
    tree = _parse_module(router_file)
    if tree is None:
        return ''
    for prefix in _collect_api_routers(tree).values():
        if prefix:
            return prefix
    return ''


def _extract_extend_routes(plugin_dir: Path, manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """
    提取扩展级插件的路由

    :param plugin_dir: 插件目录
    :param manifest: 插件清单
    :return:
    """
    extend = manifest.get('app', {}).get('extend', '')
    api_config = manifest.get('api', {})
    api_dir = plugin_dir / 'api'
    app_prefix = _get_app_prefix(extend)
    routes: list[tuple[str, str]] = []

    for api_file in sorted(api_dir.rglob('*.py')):
        if api_file.name == '__init__.py':
            continue
        tree = _parse_module(api_file)
        if tree is None:
            continue

        relative_dir = api_file.parent.relative_to(api_dir)
        target_prefix = _import_module_prefix(f'backend.app.{extend}.api.{relative_dir.as_posix().replace("/", ".")}')
        prefix = api_config.get(api_file.stem, {}).get('prefix', '')
        module_prefix = _collect_api_routers(tree).get('router', '')
        for method, path in _collect_routes(tree):
            routes.append((method, f'{app_prefix}{target_prefix}{prefix}{module_prefix}{path}'))

    return routes


def _extract_app_routes(plugin_dir: Path, manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """
    提取应用级插件的路由

    :param plugin_dir: 插件目录
    :param manifest: 插件清单
    :return:
    """
    router_file = plugin_dir / 'api' / 'router.py'
    tree = _parse_module(router_file)
    if tree is None:
        return []

    routers = _collect_api_routers(tree)
    includes = _collect_includes(tree)
    imports = _collect_router_imports(tree)
    routes: list[tuple[str, str]] = []

    for router_name in manifest.get('app', {}).get('router', []):
        router_prefix = routers.get(router_name, '')
        for alias, include_prefix in includes.items():
            module_path = imports.get(alias)
            if not module_path:
                continue
            module_file = plugin_dir / Path(*module_path.split('.')[3:]).with_suffix('.py')
            module_tree = _parse_module(module_file)
            if module_tree is None:
                continue
            module_prefix = _collect_api_routers(module_tree).get('router', '')
            for method, path in _collect_routes(module_tree):
                routes.append((method, f'{router_prefix}{include_prefix}{module_prefix}{path}'))

    return routes


def _extract_plugin_routes(plugin_dir: Path, manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """
    提取插件的路由

    :param plugin_dir: 插件目录
    :param manifest: 插件清单
    :return:
    """
    if manifest.get('app', {}).get('extend'):
        return _extract_extend_routes(plugin_dir, manifest)
    if manifest.get('app', {}).get('router'):
        return _extract_app_routes(plugin_dir, manifest)
    return []


def _collect_core_routes() -> set[tuple[str, str]]:
    """
    收集核心应用的路由

    核心应用模块延迟导入，避免与插件模块产生循环依赖

    :return:
    """
    from importlib import import_module

    try:
        module = import_module('backend.app.router')
    except Exception:
        return set()

    routes: set[tuple[str, str]] = set()
    for route in getattr(module.router, 'routes', []):
        path = getattr(route, 'path', None)
        methods = getattr(route, 'methods', None)
        if not path or not methods:
            continue
        routes.update((method, path) for method in methods)
    return routes


def get_route_conflicts(plugin_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """
    检测插件路由与现有路由的冲突

    :param plugin_dir: 插件目录
    :param manifest: 插件清单
    :return:
    """
    candidate_routes = _extract_plugin_routes(plugin_dir, manifest)
    if not candidate_routes:
        return []

    existing_routes = _collect_core_routes()
    for plugin in get_plugins():
        if plugin == plugin_dir.name:
            continue
        plugin_dir_existing = PLUGIN_DIR / plugin
        manifest_existing = rtoml.loads((plugin_dir_existing / 'plugin.toml').read_text(encoding='utf-8'))
        existing_routes.update(_extract_plugin_routes(plugin_dir_existing, manifest_existing))

    conflicts = []
    for method, path in candidate_routes:
        if (method, path) in existing_routes:
            conflicts.append(f'{method} {path}')
    return sorted(set(conflicts))


def _extract_table_names(model_dir: Path) -> set[str]:
    """
    提取模型目录中的数据表名

    :param model_dir: 模型目录
    :return:
    """
    if not model_dir.is_dir():
        return set()

    table_names: set[str] = set()
    for model_file in sorted(model_dir.rglob('*.py')):
        tree = _parse_module(model_file)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, ast.Assign):
                    continue
                if not any(isinstance(target, ast.Name) and target.id == '__tablename__' for target in item.targets):
                    continue
                if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                    table_names.add(item.value.value)
    return table_names


def _collect_existing_tables(plugin_name: str) -> set[str]:
    """
    收集现有数据表名

    :param plugin_name: 待检测插件名称
    :return:
    """
    tables = _extract_table_names(BASE_PATH / 'app')
    for plugin in get_plugins():
        if plugin == plugin_name:
            continue
        tables.update(_extract_table_names(PLUGIN_DIR / plugin / _MODEL_DIR))
    return tables


def get_table_conflicts(plugin_dir: Path) -> list[str]:
    """
    检测插件数据表与现有数据表的冲突

    :param plugin_dir: 插件目录
    :return:
    """
    table_names = _extract_table_names(plugin_dir / _MODEL_DIR)
    if not table_names:
        return []

    return sorted(table_names & _collect_existing_tables(plugin_dir.name))


def _read_sql_literal(text: str, index: int) -> tuple[str, int]:
    """
    读取 SQL 字符串字面量

    :param text: SQL 文本
    :param index: 起始位置（引号所在位置）
    :return:
    """
    chars: list[str] = []
    index += 1
    while index < len(text):
        char = text[index]
        if char == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                chars.append("'")
                index += 2
                continue
            return ''.join(chars), index + 1
        chars.append(char)
        index += 1
    return ''.join(chars), index


def _split_sql_values(text: str) -> list[list[str]]:
    """
    拆分 SQL 插入语句的值列表

    :param text: 值语句
    :return:
    """
    tuples: list[list[str]] = []
    current: list[str] = []
    field: list[str] = []
    depth = 0
    index = 0

    while index < len(text):
        char = text[index]
        if char == "'":
            literal, index = _read_sql_literal(text, index)
            field.append(literal)
            continue
        if char == '(':
            depth += 1
            if depth == 1:
                current, field = [], []
                index += 1
                continue
        elif char == ')':
            depth -= 1
            if depth == 0:
                current.append(''.join(field).strip())
                tuples.append(current)
                current, field = [], []
                index += 1
                continue
        elif char == ',' and depth == 1:
            current.append(''.join(field).strip())
            field = []
            index += 1
            continue
        field.append(char)
        index += 1

    return tuples


def _extract_menu_identifiers(sql: str) -> tuple[set[str], set[str]]:
    """
    提取 SQL 脚本中 sys_menu 的名称与权限标识

    :param sql: SQL 脚本内容
    :return:
    """
    names: set[str] = set()
    perms: set[str] = set()

    for match in _MENU_INSERT_PATTERN.finditer(sql):
        columns = [column.strip() for column in match.group(1).split(',')]
        for values in _split_sql_values(match.group(2)):
            if len(values) != len(columns):
                continue
            row = dict(zip(columns, values))
            name = row.get('name', '')
            perms_value = row.get('perms', '')
            if name and name != 'null':
                names.add(name)
            if perms_value and perms_value != 'null':
                perms.add(perms_value)

    return names, perms


def _collect_existing_menu_identifiers(plugin_name: str) -> tuple[set[str], set[str]]:
    """
    收集现有菜单名称与权限标识

    :param plugin_name: 待检测插件名称
    :return:
    """
    names: set[str] = set()
    perms: set[str] = set()

    sql_files = [POSTGRESQL_SCRIPT_DIR / 'init_test_data.sql']
    for plugin in get_plugins():
        if plugin == plugin_name:
            continue
        sql_files.append(PLUGIN_DIR / plugin / 'sql' / 'postgresql' / 'init.sql')

    for sql_file in sql_files:
        if not sql_file.is_file():
            continue
        file_names, file_perms = _extract_menu_identifiers(sql_file.read_text(encoding='utf-8'))
        names.update(file_names)
        perms.update(file_perms)

    return names, perms


def get_menu_conflicts(plugin_dir: Path) -> list[str]:
    """
    检测插件菜单与权限标识的冲突

    :param plugin_dir: 插件目录
    :return:
    """
    sql_file = plugin_dir / 'sql' / 'postgresql' / 'init.sql'
    if not sql_file.is_file():
        return []

    names, perms = _extract_menu_identifiers(sql_file.read_text(encoding='utf-8'))
    if not names and not perms:
        return []

    existing_names, existing_perms = _collect_existing_menu_identifiers(plugin_dir.name)
    conflicts = [f'菜单名称 {name}' for name in sorted(names & existing_names)]
    conflicts.extend([f'权限标识 {perms_value}' for perms_value in sorted(perms & existing_perms)])
    return conflicts
