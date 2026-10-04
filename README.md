# H3 剧集级全自动生成器

V0.1 提供跨平台任务编译和 Mock 调度核心；V0.2 增加受限的 Windows 真实 ComfyUI 集成。

已实现 TXT Prompt 自动解析、资产索引、整集预检、动态 Reference、顺序执行、保守自愈、失败隔离、运行记录和状态日志。旧 YAML/JSON 清单继续兼容。Python 3.12+；唯一运行依赖为 PyYAML，测试使用 pytest。

**已验证：Windows RTX 5070、工作室 ComfyUI、MiniMax H3 生产 API workflow 的三镜头连续真实闭环。**

## 安装与运行

本次开发已在项目的 `.venv` 中安装依赖。Mac 直接进入本项目目录后执行：

```bash
source .venv/bin/activate
pytest
python -m h3_worker.cli dry-run fixtures/manifests/episode_test.json
python -m h3_worker.cli run fixtures/manifests/episode_test.json --mock
```

新环境使用 Python 3.12 或更新版本建立独立虚拟环境：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
```

Windows 建立虚拟环境可用 `py -3.12 -m venv .venv`，安装和执行时使用 `.venv/Scripts/python.exe`。虚拟环境不可从 Mac 直接复制到 Windows；核心使用 `pathlib.Path`，没有 Windows API、PowerShell 或 GPU 依赖。

## Windows 一键下载与测试

GitHub 仓库：`MasAshlia/h3-batch-worker`，为私有仓库，Windows 使用有权限的账号登录。

**首次准备**：安装 [Python 3.12+](https://www.python.org/downloads/windows/)，启用 Python launcher 或勾选 Add Python to PATH。自动下载方式还需安装 [Git for Windows](https://git-scm.com/downloads/win)。已有 ComfyUI 不代表系统已安装可用于创建 venv 的标准 Python。

两种使用方式：

1. **一键下载并测试**：在 GitHub 打开 `scripts/windows/download-and-test.cmd`，用 **Download raw file** 保存该文件，双击运行。脚本把 `main` 分支下载到用户的 `Downloads/h3-batch-worker`，首次访问私有仓库时按 Git 提示登录 GitHub，然后自动运行测试。目标目录已存在时会停止，避免覆盖已有配置或任务。
2. **无需 Git**：登录 GitHub，在仓库点击 **Code → Download ZIP**，解压后双击根目录的 `test-windows.cmd`。

测试入口自动创建项目自己的 `.venv`，安装 `.[test]`，依次执行 pytest、20 条 fixture dry-run、20 条 mock 批次，并验证新生成的 `run.json` 和 `run.log`。成功显示 `H3 TEST PASS`；失败显示对应阶段错误且返回非 0，窗口会保留方便查看。使用示例配置，不覆盖工位的 `config.yaml` 或修改 ComfyUI 环境。

后续更新可以在干净的 Git 工作目录执行 `git pull --ff-only`，再双击 `test-windows.cmd`。ZIP 方式重新下载到新目录即可。

这些入口执行的是 **Windows 逻辑与 mock 冒烟测试**。真实 workflow 和 GPU 生成按本文的 Windows 集成步骤处理；Mac 验证不能代替 Windows 实机验证。

默认读取当前目录中的 `config.yaml`；不存在时读取 `config.example.yaml`。可复制示例为 `config.yaml`，或者显式指定：

```bash
python -m h3_worker.cli dry-run fixtures/manifests/episode_test.json --config config.example.yaml
```

CLI 的 `run` 必须明确指定 `--mock` 或 `--real`。`--real` 默认仅允许一个 shot；只有显式添加 `--max-real-shots 2` 或 `3` 才放宽本次调用，且硬上限为 3。真实模式执行服务、队列、workflow、输出与 history preflight，逐条串行、零重试；镜头之间若有外部任务插入则有界等待队列重新空闲，任一生成失败立即停止后续提交。

## Prompt 固定格式

```text
duration: 5
@image_1: 林默
@image_2: 客厅

@image_1 走进 @image_2，停下并看向窗外。镜头缓慢推进。
```

- 元数据必须放在头部；首个空行结束头部。正文前使用空行分隔。
- `duration` 必须出现一次，为正整数秒数；可写 `5`、`5s`、`5 秒`。V0.1 不猜测小数、范围或自然语言中的时长。
- 声明至少一个、最多九个引用，编号限 `@image_1`～`@image_9`；编号可稀疏，重复声明报错。
- 正文出现的每个 `@image_X` 必须有声明；不允许 `@image_0`、`@image_10` 等编号。
- 完整 Prompt 保留并写入 workflow，不剥离头部或改写正文。
- `@image_1 → ref_image_0`，`@image_9 → ref_image_8`。映射按编号计算，绝不按出现次序重新编号。
- `duration: 5 → Float 5.0 / steps 15`；`duration: 8 → Float 8.0 / steps 24`。
- 缺时长、缺引用、含糊格式分别提供 `MissingDurationError`、`MissingReferenceError`、`InvalidPromptFormatError`，可调用 `to_dict()` 获取结构化错误。

## 清单与路径

`fixtures/manifests/episode_test.json` 包含虚构的 `EP_TEST`，共 20 条任务。镜头编号和 episode 使用英文字母、数字、下划线或连字符；镜头编号必须唯一。

```json
{
  "episode": "EP_TEST",
  "shots": [
    {"shot_id": "S001", "prompt_file": "../prompts/S001.txt"},
    {"shot_id": "S002", "prompt_file": "../prompts/S002.txt"}
  ]
}
```

资产清单 `fixtures/assets/asset_manifest.json`：

```json
{
  "林默": "linmo.png",
  "客厅": "living_room.png",
  "苏晴": "suqing.png",
  "公司": "office.png"
}
```

路径解析规则固定：配置中的路径相对于配置文件；`prompt_file` 相对于 episode 清单；资产路径相对于资产清单。也支持本平台的绝对路径。建议清单使用 `/` 分隔的相对路径，便于 Mac/Windows 迁移。

同名 JSON 键会报错，不允许静默覆盖资产定义。资产清单中的所有文件都检查存在性，包括尚未使用的资产。不存在的引用、文件、无效清单、重复镜头编号或错误 mapping 会让整批预检失败，**不会提交已通过的子集**。可以从任何工作目录通过绝对清单路径和 `--config` 执行。

可在单个 shot 上添加非负整数 `seed`；此时必须配置 `seed_node`，否则预检报错，避免静默忽略 seed。没有指定 seed 时保留模板原值。

## Workflow 测试替身及配置

`fixtures/workflows/sample_h3_api_workflow.json` 的节点标题明确标记 **TEST FIXTURE ONLY**。

它只包含测试用 Input Text、Reference、Duration Float、Scheduler 和一个用于证明无关字段保持不变的节点。`Test*` class_type 不是真实 H3 或 ComfyUI 节点，**不能提交到真实 ComfyUI**。四张有效 PNG 为纯色虚构资产；未使用工作室剧本、图片或模型。

默认 mapping：

```yaml
workflow:
  template: ./fixtures/workflows/sample_h3_api_workflow.json
  mapping:
    prompt_node: "100"
    reference_node: "200"
    duration_node: "300"
    scheduler_node: "400"
```

默认输入名分别为 `text`、`ref_image_0`～`ref_image_8`、`value`、`steps`。可以通过 `prompt_input`、`duration_input`、`steps_input` 改名。可选 seed 使用 `seed_node` 和 `seed_input`（默认 `seed`）。节点 id 必须是加引号的字符串，输入必须已存在于 API 模板中；错误和拼错的 mapping 都会被拒绝。

如果真实 workflow 的 Reference 输入是节点连接，应把上传文件名写入对应图片加载节点的字面量输入，配置如下，**其中 501/502 只是说明用编号，必须替换为真实模板编号**：

```yaml
mapping:
  prompt_node: "100"
  duration_node: "300"
  scheduler_node: "400"
  reference_inputs:
    ref_image_0: {node: "501", input: image}
    ref_image_1: {node: "502", input: image}
```

显式 `reference_inputs` 取代默认 `reference_node`，只配置模板实际支持的槽位。核心禁止把已有 `[node_id, output_index]` 连接覆盖为字符串；连接保留在模板里。所有任务每次都从模板深拷贝，不修改无关字段；未使用的已映射引用输入清空，避免模板样例图片泄漏到下一任务。

Dry-run 会构建全部 workflow 字典，本地资产使用完整路径；运行时先上传，再按服务端返回的文件名重新 patch，不能把 Mac 本地路径直接作为远端 ComfyUI 文件名。这里的 `workflow: PASS` 仅表示本地结构和目标输入检查通过。

真实模板的可选槽位、未使用图片加载节点能否接受空值、输入类型和 H3 时长限制，需要 Windows 工位验证；如果真实节点输入契约不同，不能仅凭本地 PASS 认定集成完成。

## Mock 队列、失败重试和记录

`MockComfyClient` 模拟上传、提交、队列、历史和等待完成；无网络和 GPU。每条任务默认模拟运行 0.1 秒，可在 Python 接口设为 0.1～0.5 秒。提交后处于 `PENDING`，随后为 `RUNNING`，最终 `SUCCESS` 或 `FAILED`。

Queue Manager 顺序提交，每条完成后再处理下一条。状态包含 `PENDING / VALIDATED / SUBMITTED / RUNNING / SUCCESS / FAILED / RETRY`。首次执行失败后重新上传和提交一次；第二次失败记为 FAILED，继续后续镜头。配置错误只在预检阶段处理，不进入重试。

```bash
# 第三条两次都失败，完成后得到 19 success / 1 failed，退出码为 1
python -m h3_worker.cli run fixtures/manifests/episode_test.json --mock --mock-fail S003

# 第三条仅第一次失败，重试后成功，得到 20 success / 0 failed
python -m h3_worker.cli run fixtures/manifests/episode_test.json --mock --mock-fail-once S003
```

运行生成 `runs/<batch_id>/run.json` 和 `run.log`。JSON 包含 batch 时间、每条镜头完整 Prompt、原始 references、内部槽位、duration、steps、seed、最终 prompt_id、status、retry_count、开始/结束时间、结构化 error。`attempts` 保留两次尝试各自的 id 和错误，`events` 保留状态迁移。最终 summary 统计 success/failed。UTC ISO 时间写入 JSON，日志使用运行机器本地时间。

每次状态更新都通过临时文件替换保存 JSON，避免留下半写 JSON。日志只输出镜头编号、状态和 prompt_id，不输出 Token、密码、完整 Prompt、本机绝对路径或原始 HTTP 错误正文。运行记录按需求保存完整 Prompt，请使用虚构或获准使用的任务内容。

Dry-run 配置错误、预检错误、mock 最终失败时返回非 0；全批成功返回 0。V0.1 没有崩溃后自动恢复或断点续跑，记录用于追查；进程中断后的重跑属于一个新批次。

## 模块与测试

| 模块 | 职责 |
| --- | --- |
| `models.py` | ShotTask、状态和编译结果 |
| `parser.py` / `validator.py` | 固定格式提取和规则验证 |
| `asset_resolver.py` | 资产名解析、冲突和文件检查 |
| `workflow_patcher.py` | 可配置节点/输入、深拷贝、引用编号映射 |
| `batch.py` | episode 清单读取及整批编译 |
| `config.py` / `file_io.py` / `errors.py` | 配置、文件读取和结构化错误 |
| `comfy_client.py` | 抽象接口、完整 mock、基础 HTTP adapter |
| `queue_manager.py` | Mock 重试、真实受控串行提交、history 输出映射、日志和运行记录 |
| `cli.py` | dry-run、mock run 和最多三镜头的显式受限 real run |

`tests/unit` 覆盖解析边界、1/9/超过9张引用、off-by-one、缺失/冲突资产、时长与 steps、模板隔离、不同 mapping、seed、配置和 HTTP 传输契约。`tests/integration_mock` 覆盖 20 条批次、第三条持续失败后继续第四条、一次失败后成功、整批拒绝提交、CLI 退出码和生成 run.json。测试不依赖真实服务，也不进行 GPU 推理。

## Windows 实机配置

迁移时准备以下内容并替换配置，不需要改动固定格式解析、编号映射和 mock 调度规则：

1. **真实 API 格式 workflow JSON**，不是 ComfyUI 编辑器 UI 格式；替换 `workflow.template`。
2. **实际节点编号与输入名**；替换 mapping。提供上传图片加载节点及到 Reference 的现有连接，确认可选引用槽位的行为。
3. **真实资产清单和文件**；替换 `paths.asset_manifest`、episode 清单和 Prompt 文件。
4. **真实服务地址和输出目录**；替换 `comfyui.base_url`、`paths.output_root`。

真实运行前必须在 `private/` 中保存 API workflow、节点 mapping、单镜头 Prompt、最小资产清单和本机配置；先运行 inspection 与 dry-run，再在空闲队列执行一个获准测试 shot。真实 CLI 不做失败自动重试，断线/超时后的人工核对应先查 queue/history，避免重复提交。

`RealComfyClient` 使用标准库 urllib 实现图片 multipart 上传、prompt 提交、queue/history 查询及约 1 秒轮询；路由依据 [ComfyUI 官方服务器 API 文档](https://docs.comfy.org/development/comfyui-server/comms_routes)，上传与 history 字段依据 [官方 server.py](https://github.com/Comfy-Org/ComfyUI/blob/master/server.py) 和 [execution.py](https://github.com/Comfy-Org/ComfyUI/blob/master/execution.py) 核对（2026-10-01）。V0.2 已验证本机单镜头，不提供视频下载、鉴权、多镜头恢复或批量生产承诺。

## Windows 受控真实集成

V0.2 的 `--real` 安全阀默认只接受一个 shot，并要求真实 API-format workflow、空闲且可读的 ComfyUI 队列，以及存在的 video output 节点。获准的连续测试可显式使用 `--max-real-shots 3`，但不能超过 3；每条提交前等待队列为空，任务串行且不重试，失败后不再提交后续 shot。图片先上传，提交 workflow 只写服务器返回文件名。完成后必须从真实 history 找到唯一视频并验证本地输出非空，否则该 shot 记为失败。

生产 Prompt 若不包含 V0.1 的 `duration:` / `@image_n:` 头部，不改写 Prompt；在私有 episode manifest 中显式提供确定性元数据：

```json
{
  "episode": "REAL_TEST",
  "shots": [{
    "shot_id": "S001",
    "prompt_file": "test_prompt.txt",
    "duration": 5,
    "references": {"image_1": "reference_1"}
  }]
}
```

私有配置、workflow、Prompt、资产清单、history 与视频必须放在已忽略的 `private/` 或 `local/`。先执行：

```text
python -m h3_worker.cli dry-run private/test_episode.json --config private/config.windows.yaml
python -m h3_worker.cli run private/test_episode.json --real --config private/config.windows.yaml
# 仅在清单确有三条获准任务时：
python -m h3_worker.cli run private/test_episode_3shot.json --real --max-real-shots 3 --config private/config.windows.yaml
```

真实运行记录包含 `shot_id`、`prompt_id`、状态时间戳和 history 提取的 output 映射。公开报告只使用 `docs/WINDOWS_REAL_INTEGRATION_REPORT.example.md` 模板。

## 人工投喂并持续生成

双击根目录的 `start-continuous.cmd` 会启动真实 ComfyUI 收件箱。每集建立一个 `EP33.ready` 文件夹，每个 shot 放一份 TXT；第一行必须是 `SHOT 33-16 标题`。TXT 全部保存完成后才把文件夹改成 `.ready`，程序会先验证同一集、镜头号唯一并按镜头号排序，再原子领取为 `.processing`。旧的 `.ready.yaml` / `.ready.json` 仍可使用。

标准制作 Prompt 的原始时长必须是 5～9 秒；`SHOT DURATION: 5.88 seconds` 会向上取整为 duration 6，并生成 steps 18。`[UPLOAD MAPPING]` 中的 `Asset R01-V01 = <Picture 1>` 等行会自动转换为 Reference。上一镜头尾帧暂不提取，程序会删除该输入并把剩余静态 Picture 连续重编号，确保 Prompt 编号与实际上传顺序一致。

资产先查 `asset_manifest.json` 的人工映射，再查带 SHA-256 的自动索引，最后扫描配置的 `asset_roots`。只有一个规范化文件名前缀匹配时才自动采用；零命中或多命中都会在上传前拒绝整集。程序启动及每集预检读取 ComfyUI 的 `MiniMaxH3ReferenceToVideo` schema，按实际资产数创建 `LoadImage` 节点并连接 `ref_images.ref_image_1…N`，当前服务端上限为 9。

程序先完成整集预检，然后逐镜头等待生产队列空闲、上传 Reference、提交 workflow、等待 history 和验证视频输出。安全的 queue/history/schema/上传读取错误最多短暂重试三次；收到 `prompt_id` 后绝不重复提交，无法确认提交结果时记为 `UNKNOWN_SUBMISSION` 并停止本集。一个镜头失败会停止该集剩余镜头，但 watcher 会继续下一集。

处理成功的输入移动到 `private/inbox/done`，失败的输入移动到 `private/inbox/failed`；失败的剧集目录内会生成 `diagnostics.json`，保存错误类别、资产候选、已做修复和安全的人工处理方式。每次运行的 `run.json` 和独立 history 文件保存在配置的 `output_root/<batch_id>/`。进程意外中断时 `.processing` 不会自动重投；应先核对 ComfyUI queue/history 和最近的 run 记录，再由人工决定是否改回 `.ready`，避免重复生成。

也可以从终端启动或只处理当前已就绪文件：

```text
python -m h3_worker.cli watch private/inbox --real --config private/config.windows.yaml
python -m h3_worker.cli watch private/inbox --mock --once --config config.example.yaml
```

## 本地可视化生成台

双击 `start-ui.cmd` 会打开 `http://127.0.0.1:8765/`。界面只绑定本机地址，支持拖入或选择单个总 TXT、多份分镜 TXT、原有 YAML/JSON，也可直接粘贴 Prompt。点击“分析并检查资产”后会显示每个镜头的时长换算、Reference 槽位、Asset ID、实际文件与缩略图；所有镜头通过预检后才能生成。

每个镜头可独立设置生成 1～4 次，默认一次。程序仍按镜头顺序串行执行，同一镜头的多个版本先连续完成，再进入下一镜头。版本输出使用 `EPxx_Sxx_V01` 形式区分；生成次数是明确的新任务次数，运行错误不会偷偷增加生成次数。进度区显示上传、入队、生成、保存、完成或失败状态。需要人工介入时，页面顶部立即显示错误类型、ComfyUI 节点和安全诊断；当前集停止，未运行项目标记为跳过。

右侧历史列表读取既有 `run.json`，下方可直接播放已经落盘的视频，因此之前完成的真实 EP34 九个镜头也会自动出现在页面中。关闭启动窗口即可停止界面服务，不影响已经由 ComfyUI 完成的视频文件。

此版本没有多机调度、废片识别、剪映集成、数据库或模型安装。项目作为独立的 `h3-batch-worker` 仓库发布；`.venv`、`runs`、`.env`、本地 `config.yaml` 和缓存不上传。
