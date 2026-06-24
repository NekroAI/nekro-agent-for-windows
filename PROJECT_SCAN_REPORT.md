# Nekro Agent Windows Launcher 三轮扫描报告

生成时间：2026-06-24
扫描范围：当前工作树 `D:\System\Desktop\na_for_windows`
目标：全面扫描项目三次，指出设计缺陷与隐藏 bug，并写入 Markdown。

## 扫描方法

本次按三轮独立视角扫描：

1. **运行时/架构/配置扫描**：WSL、Docker、Compose、镜像、迁移、配置持久化。
2. **UI/线程/状态流扫描**：首次运行向导、迁移向导、主窗口、多实例切换、后台线程生命周期。
3. **打包/测试/静态风险扫描**：PyInstaller/Inno Setup、更新器、测试与 `uv run poe lint`。

当前工作树在扫描前已有修改：

- `core/wsl/images.py`
- `tests/test_wsl_images.py`
- `ui/first_run_dialog.py`

报告以下发现均基于当前工作树，不假设这些改动来自谁。

## 自动检查结果

以下为扫描时结果；相关问题已在修复后重新验证，见下一节。

已运行项目声明的检查命令：

```powershell
uv run poe lint
```

结果：失败。

- `compileall`：通过
- `unittest discover`：通过，24 个测试通过
- `pyright`：失败
- unused imports：通过
- dangerous calls：通过
- line endings and whitespace：通过
- `git diff --check`：通过

Pyright 失败位置：

- `tests/test_wsl_images.py:85`
- `tests/test_wsl_images.py:86`
- `tests/test_wsl_images.py:87`

原因：`WSLImageMixin._parse_auth_challenge()` 在 `core/wsl/images.py:200` 可能返回 `None`，测试在 `tests/test_wsl_images.py:85-87` 直接对 `params["realm"]` 等字段下标访问。运行时单测能过，但类型门禁失败，当前仓库无法通过项目自己的 lint gate。

## 修复后状态

2026-06-24 已按本报告修复主要 P1/P2 问题，并重新运行：

```powershell
uv run poe lint
```

结果：通过。

- `compileall`：通过
- `unittest discover`：通过，28 个测试通过
- `pyright`：通过，0 errors
- unused imports：通过
- dangerous calls：通过
- line endings and whitespace：通过
- `git diff --check`：通过

已修复的主要问题：

- 新建实例和设置页保存端口时接入实例间端口冲突校验。
- 向导后台任务运行时不再假装取消，关闭会提示等待任务完成。
- 接管迁移不再接受 `tar` 警告返回码，关键目录缺失或整理失败会中断。
- WSL 部署目录删除前增加托管路径和 Compose 标记校验。
- Pyright optional subscript 失败已修复。
- 镜像状态/单镜像更新改为复用统一镜像引用解析。
- active instance 兼容字段、预览备份状态改为批量/同步更新。
- 镜像测速完成后不再自动倒计时进入部署。

## 高优先级问题

### P1. 新建/编辑实例端口时没有校验“实例间”冲突

证据：

- 首次运行/新实例向导只调用 `validate_port_bindings()`：`ui/first_run_dialog.py:1035-1038`
- 设置页保存端口也只调用 `validate_port_bindings()`：`ui/main_window.py:2819-2821`
- 项目已有实例级校验器 `validate_instance_port_conflicts()`：`core/port_utils.py:104`
- 启动所有实例时才做实例级校验：`core/wsl/deploy.py:385-388`
- 测试也覆盖了实例端口冲突：`tests/test_port_utils.py:38-89`

影响：

用户可以在另一个实例未运行时，把新实例或当前实例端口改成已有实例端口。保存时本机端口可能可绑定，因此通过；到下次启动所有实例时才失败。这会把错误从输入阶段推迟到启动阶段，表现为“配置已保存但启动失败”。

建议：

在 `FirstRunDialog._confirm_datadir()` 和 `MainWindow._save_ports()` 中增加 `validate_instance_port_conflicts(self.config.list_instances(), port_specs, current_instance_id=...)`，并在保存前阻断冲突。

### P1. 向导后台 QThread 不能真正取消，关闭窗口后任务仍可能继续跑

证据：

- 统一关闭逻辑只调用 `thread.quit()` 和 `thread.wait(3000)`：`ui/widgets.py:331-338`
- 这些线程重写了 `run()`，直接执行阻塞任务，不跑 Qt event loop：
  - `CreateRuntimeThread.run()`：`ui/widgets.py:279-284`
  - `CheckStepThread.run()`：`ui/first_run_dialog.py:43-48`
  - `ImageSpeedTestThread.run()`：`ui/first_run_dialog.py:61-67`
- 迁移向导也复用同一跟踪机制：`ui/migration_dialog.py:159`、`ui/migration_dialog.py:344`、`ui/migration_dialog.py:445`

影响：

用户在下载 rootfs、创建发行版、镜像测速、迁移数据时关闭对话框，`quit()` 不会停止正在运行的阻塞函数。3 秒后 `_active_threads` 被清空，后台任务仍可能继续修改 WSL、Docker 或配置；严重时还可能触发 PyQt 的 “QThread destroyed while thread is still running” 类问题。

建议：

给这些任务增加显式取消令牌，并让后端长任务周期性检查；对不能取消的外部命令，关闭按钮应变为“后台继续/不可取消”的明确状态，不能假装取消。

### P1. 接管/迁移流程可能把“不完整归档”当成功

证据：

- 打包数据使用 `tar --ignore-failed-read` 且丢弃 stderr：`core/wsl/discovery.py:580-586`
- `tar` 返回码 1 被当作成功：`core/wsl/discovery.py:588-616`
- 还原同样接受返回码 1：`core/wsl/discovery.py:697-723`
- 目录搬迁失败只记录 warn，不中断接管：`core/wsl/discovery.py:413-430`

影响：

当数据目录、Docker volume 或部署目录有文件无法读取时，迁移仍可能显示完成。用户之后启动的是缺数据的实例，问题会表现为数据库缺失、配置丢失或服务异常，而不是在迁移阶段明确失败。

建议：

迁移归档应列出关键目标并逐项验证；核心目录缺失或 `tar` 返回码非 0 时默认失败。只有已知可选目标缺失时才允许降级，并把缺失项展示给用户确认。

### P1. 删除部署目录使用配置路径直接 `rm -rf`，缺少范围校验

证据：

- 卸载环境删除 active `deploy_dir`：`core/wsl/deploy.py:745-748`
- 移除单实例删除 `inst_data["deploy_dir"]`：`core/wsl/deploy.py:844-849`
- 接管搬迁 fallback 也会删除源路径：`core/wsl/discovery.py:416-430`

影响：

路径有 `shlex.quote`，但没有验证它是否仍是受管理的 Nekro Agent 部署目录。如果 `config.json` 损坏、被手动改写，或接管扫描得到异常路径，启动器可能在 WSL root 下删除错误目录。

建议：

删除前必须验证目标满足所有条件：非空、位于允许前缀、包含预期 Compose/.env 标记、不是 `/`/`/root`/`/home` 等高危路径。删除失败和校验失败应明确提示。

### P1. 当前工作树无法通过 Pyright

证据：

- `_parse_auth_challenge()` 可返回 `None`：`core/wsl/images.py:200-211`
- 测试直接下标访问返回值：`tests/test_wsl_images.py:80-87`
- `uv run poe lint` 输出 3 个 `reportOptionalSubscript` 错误。

影响：

项目自己的 lint gate 当前失败。即使单元测试通过，CI 或本地提交前检查会被阻断。

建议：

在测试中先 `self.assertIsNotNone(params)`，再用 `assert params is not None` 或局部变量窄化类型后访问字段。

## 中优先级问题

### P2. 镜像状态/单镜像更新仍在手写拆分镜像名，绕过新解析器

证据：

- 拉取测速路径已有 `_normalize_image_ref()`、`_registry_manifest_target()` 等解析逻辑：`core/wsl/images.py:119-190`
- 镜像状态检查仍使用 `image_ref.split(":")`：`core/wsl/images.py:720-721`
- 单镜像拉取也使用 `split(":")`：`core/wsl/images.py:798-801`
- 远程状态检查硬编码 Docker Hub token 和 manifest URL：`core/wsl/images.py:744-758`

影响：

当前常量大多是 Docker Hub 简单引用，所以短期不一定触发。但一旦管理镜像变成 `registry:port/repo:tag`、digest、非 Docker Hub registry，状态检查和单镜像更新会误拆镜像名，或仍去 Docker Hub 查询错误仓库。

建议：

统一复用 `_registry_manifest_target()` 和 `_normalize_image_ref()`；远程检查按 registry challenge 获取 token，而不是只支持 Docker Hub。

### P2. 多字段配置更新不是原子事务，实例切换可能落入半同步状态

证据：

- `ConfigManager.set()` 每设置一个 key 就立即写盘：`core/config_manager.py:191-194`
- 切换 active instance 连续写多个全局兼容字段：`ui/main_window.py:1900-1909`
- 启动所有实例时也重复逐字段写：`core/wsl/deploy.py:407-418`、`core/wsl/deploy.py:437-455`

影响：

如果写盘在中途失败，`active_instance`、端口、发布频道、`deploy_info` 可能互相不匹配。UI 和后端依赖这些兼容字段，可能打开错误端口、显示错误凭据，或对错误实例执行更新。

建议：

给 `ConfigManager` 增加批量更新方法，在锁内一次性更新多个字段并只保存一次。实例切换、启动恢复、新建实例、回滚都应走同一个同步函数。

### P2. 配置 JSON 损坏时直接回退默认值，后续保存可能覆盖真实状态

证据：

- JSON decode 失败只记录日志并返回默认配置：`core/config_manager.py:149-159`

影响：

如果 `config.json` 半写入、被外部编辑坏、磁盘异常导致损坏，启动器会以首次运行/默认状态启动。之后任何保存都可能覆盖原配置，导致实例列表、端口、凭据、更新状态丢失。

建议：

读取失败时把坏文件重命名为 `.corrupt.<timestamp>.json`，向用户显示可恢复路径，并避免在用户确认前覆盖。能恢复字段时应尽量保留。

### P2. 预览版备份状态的全局兼容字段可能变旧

证据：

- `set_active_preview_backup_available()` 有 active instance 时只更新实例字段：`core/config_manager.py:267-274`
- 预览版切换/恢复又分别更新实例字段：`core/wsl/update.py:315-322`、`core/wsl/update.py:468-475`
- 项目规则要求当前行为依赖时同时更新全局兼容字段和选中实例。

影响：

目前 UI 多数路径走 `get_active_preview_backup_available()`，所以不一定马上错。但全局 `preview_backup_available` 会滞后，任何旧代码、兼容逻辑或未来功能读取 `config.get("preview_backup_available")` 时会得到错误状态。

建议：

`set_active_preview_backup_available()` 应同时写全局兼容字段和 active instance，且只保存一次。

### P2. 镜像测速完成后 5 秒自动继续部署，用户查看详情也不会暂停

证据：

- 详情按钮只切换详情可见性：`ui/first_run_dialog.py:790-795`
- 测速成功后总是启动倒计时：`ui/first_run_dialog.py:846-886`
- 倒计时到 0 直接 `_continue_after_speedtest()`：`ui/first_run_dialog.py:806-817`

影响：

测速详情是为了让用户判断镜像源问题，但用户点开详情阅读时仍会自动进入部署。网络不稳定、无可用源、或用户想返回修改配置时，流程可能先一步开始写入配置/拉取镜像。

建议：

打开详情、点击窗口、或存在失败源时取消自动继续；或者把自动继续改成显式用户设置，不默认启用。

### P2. 安装器卸载会删除启动器状态，但不会清理 WSL/Docker 运行时

证据：

- Inno 卸载只 taskkill 启动器并执行 `CleanupLauncherState()`：`installer.iss:111-124`
- `CleanupLauncherState()` 删除安装目录 data、`_internal` 和 `%LOCALAPPDATA%\NekroAgent`：`installer.iss:83-97`
- 真正的 WSL 卸载逻辑在应用内 `uninstall_environment()`：`core/wsl/deploy.py:705`

影响：

用户从 Windows“卸载程序”卸载时，WSL 发行版、Docker 容器、volume 可能仍留在机器上，但启动器配置被删除。重装后需要靠扫描/接管找回，否则会留下占空间的孤儿运行时。

建议：

安装器卸载应提示“仅卸载启动器/同时清理 WSL 运行环境”，或至少保留状态并在下次安装后提示发现旧 runtime。

## 低优先级/设计债

### P3. 更新下载线程取消会阻塞 UI，且线程对象缺少完整清理链

证据：

- 下载 worker 使用阻塞 `requests.get(..., timeout=20)`：`core/app_updater.py:252`
- 取消时 GUI 线程调用 `quit()` 和 `wait(3000)`：`ui/update_dialog.py:355-360`
- 下载完成槽也同步等待线程退出：`ui/update_dialog.py:364-366`

影响：

用户在下载卡住时关闭弹窗，界面最多会阻塞 3 秒；请求仍可能在超时前继续。虽然范围小于 WSL 创建/迁移，但体验上仍会出现“取消不立即生效”。

建议：

把 worker 的 `finished` 连接到 `thread.quit()`、`worker.deleteLater()`、`thread.deleteLater()`；取消时只置位并禁用 UI，等 worker 自然退出后再关闭或提示。

### P3. 部分 WSL 路径命令仍使用双引号拼接

证据：

- 扫描/读取外部部署路径时存在 `cat "{deploy_dir}/docker-compose.yml"` 类拼接：`core/wsl/discovery.py:232-253`
- `.env` 路径读取也使用双引号拼接：`core/wsl/discovery.py:253-270`

影响：

大多数由本项目生成的路径是安全的，但接管外部发行版时路径来自扫描结果。若路径包含双引号、命令替换字符等特殊内容，双引号不足以作为 Bash 安全 quoting。

建议：

外部路径一律使用 `shlex.quote()`，避免混用手写双引号。

## 建议修复顺序

1. 先修复 Pyright 失败，恢复 `uv run poe lint` 绿灯。
2. 在新建实例和端口设置中接入 `validate_instance_port_conflicts()`。
3. 给 WSL 删除路径增加强校验，尤其是 `remove_single_instance()` 和接管迁移。
4. 把向导 QThread 改成可取消/不可取消状态明确的生命周期模型。
5. 强化迁移归档校验，禁止把关键数据缺失当作成功。
6. 增加 `ConfigManager` 批量保存 API，统一 active instance 同步。
7. 合并镜像引用解析逻辑，避免状态检查和拉取路径各写一套。

## 结论

项目整体结构清晰，核心风险集中在多实例状态一致性、长任务取消语义、WSL 破坏性操作保护、迁移完整性校验，以及镜像引用解析的一致性。当前最直接的可验证问题是 lint gate 因 Pyright 失败而不通过；最可能影响用户数据和运行环境的问题是迁移容错过宽、`rm -rf` 路径缺少安全边界、以及实例间端口冲突保存过早放行。
