# ForgeQA

**通用造数与自动回归工具。** Python（Requests / Playwright / Faker）+ SQL，YAML 驱动，可适配任意网站。

> 一句话定位：用 YAML 描述「造什么数据、打哪个接口、查哪条 SQL、点哪个按钮」，剩下的交给引擎。
> **换站点 = 换配置，不改代码。**

```
┌── 造数 ──────────┐  ┌── 执行 ─────────────┐  ┌── 判定 ────────────┐
│ Schema 驱动       │  │ HTTP  requests      │  │ 断言算子（35+）     │
│ Faker + 派生字段  │→ │ SQL   sqlite/PG/MySQL│→ │ 失败根因四分类      │
│ 边界/异常/极端变异 │  │ UI    Playwright    │  │ 快照基线 diff       │
│ 固定 seed 可复现  │  │ 脚本  Python 逃生舱 │  │ HTML / JUnit 报告   │
└──────────────────┘  └─────────────────────┘  └────────────────────┘
```

---

## 目录

- [为什么再造一个轮子](#为什么再造一个轮子)
- [安装](#安装)
- [5 分钟跑通](#5-分钟跑通)
- [脚手架：forgeqa init 生成了什么、怎么撤销](#脚手架forgeqa-init-生成了什么怎么撤销)
- [程序入口：从哪个文件执行](#程序入口从哪个文件执行)
- [适配你自己的网站](#适配你自己的网站)
- [扫描站点：自动发现接口并生成用例](#扫描站点自动发现接口并生成用例)
- [导入接口文档：从 OpenAPI/Swagger 生成 POST 用例](#导入接口文档从-openapiswagger-生成-post-用例)
- [造数：Schema 参考](#造数schema-参考)
- [用例：YAML 参考](#用例yaml-参考)
- [配置：env.yaml 参考](#配置envyaml-参考)
- [变量与模板语法](#变量与模板语法)
- [断言算子清单](#断言算子清单)
- [断言方法清单](#断言方法清单)
- [命令行参考](#命令行参考)
- [接入 CI](#接入-ci)
- [设计取舍](#设计取舍)
- [工程结构](#工程结构)

---

## 为什么再造一个轮子

已有的方案通常各占一角：接口测试框架不管造数，造数工具不入库，UI 自动化不做数据校验。
真实回归场景要的是**一个用例里串起「造数 → 调接口 → 查库 → 点页面 → 断言 → 清理」**，
并且换一个网站时不用重写代码。ForgeQA 就是这件事。

三个明确的差异化能力：

| 能力 | 说明 |
|---|---|
| **变异造数** | 从一份正常样本自动生成边界（min-1/min/max/max+1）、异常（缺省/类型混淆/XSS/SQLi/路径穿越）、极端（64KB 串/emoji 轰炸/深层嵌套）数据。造数覆盖率提升最快的一环。 |
| **反推 Schema** | `forgeqa probe` 直接读真实接口响应，反推出可编辑的造数定义，不用从零手写字段。 |
| **用例级数据隔离** | 内置 `${uniq}`：同一用例内恒定、跨用例、跨运行都不同。业务表上的唯一约束不再被测试数据撞出假缺陷。 |

---

## 安装

**方式一：用 requirements.txt（推荐，版本已锁定）**

```bash
cd forgeqa
pip install -r requirements.txt          # 运行所需（核心 + UI + DB + 演示站点）
pip install -r requirements-dev.txt      # 额外加上测试与覆盖率工具
python -m playwright install chromium    # ← 别漏：下载浏览器内核
```

三个依赖文件的区别：

| 文件 | 内容 | 什么时候用 |
|---|---|---|
| `requirements.txt` | 顶层依赖，版本锁定为实测组合 | 日常使用 |
| `requirements-dev.txt` | 上面 + `pytest` / `pytest-cov` | 要跑单测、改工具本身 |
| `requirements.lock.txt` | 连传递依赖一起钉死（28 个包） | 换机器复现环境、排查环境差异 |

> 本工程的锁定版本实测基线：**Python 3.13.12 / macOS arm64**，277 个单测 + 端到端套件通过。全部依赖要求 Python >= 3.10。

**方式二：从源码安装（带 `forgeqa` 命令）**

```bash
git clone <this-repo> forgeqa && cd forgeqa
pip install -e ".[all]"
python -m playwright install chromium
```

**方式三：按需最小安装**

```bash
# 只要接口回归 + SQL + 造数
pip install requests PyYAML Faker

# 加上 UI 回归
pip install playwright Pillow && playwright install chromium

# 加上 MySQL / PostgreSQL 支持
pip install SQLAlchemy PyMySQL
```

依赖矩阵：

| 依赖 | 必需 | 用途 |
|---|---|---|
| `requests` | ✅ | 接口请求 |
| `PyYAML` | ✅ | 用例与配置 |
| `Faker` | ✅ | 语义化造数（中文 locale） |
| `playwright` + `Pillow` | 可选 | UI 回归、视觉像素比对 |
| `SQLAlchemy` + 驱动 | 可选 | MySQL / PostgreSQL；SQLite 走内置 `sqlite3`，零依赖 |
| `Flask` | 可选 | 仅内置演示站点需要 |

---

## 5 分钟跑通

内置了一个**有真实校验逻辑**的演示站点（字段校验、唯一约束、业务不变量、UI 表单页），
所以跑出来的结果是真有意义的，不是对着假接口自娱自乐。

```bash
# 1) 生成项目脚手架（配置 / 造数 schema / 示例用例 / 建表脚本）
forgeqa init --base-url http://127.0.0.1:8000

# 2) 启动演示站点（新开一个终端）
forgeqa demo --port 8000

# 3) 先看看造出来的数据长什么样，顺带产出变异数据集
forgeqa gen --schema config/schemas/user.yaml --count 2 --mutate --preview 2

# 4) 跑回归（首次同时建立快照基线）
forgeqa run --cases cases --baseline update --open
```

预期输出：

```
════════════════════════════════════════════════════════════════
  ForgeQA 回归结果   env=local   seed=20260922
════════════════════════════════════════════════════════════════
  总计 5   通过 5   失败 0   异常 0   跳过 0   flaky 0   通过率 100.0%
  耗时 3.2s
════════════════════════════════════════════════════════════════
  HTML 报告: out/reports/report-20260922-145113.html
```

报告是**单文件 HTML**：无外部依赖、失败截图 base64 内嵌，可直接归档给不装环境的同事。

### 验证「工具本身真的能抓到问题」

一个抓不到失败的回归体系比没有回归体系更危险。仓库自带自检用例：

```bash
forgeqa run --cases examples/selfcheck_cases
```

它会故意断言错误，报告里能看到期望值、实际值、以及根因初判（缺陷 / 环境 / 脚本 / 数据）。

---

## 脚手架：forgeqa init 生成了什么、怎么撤销

### init 会生成什么

`forgeqa init` 在当前目录（或 `--root` 指定目录）生成一份**固定清单**的项目脚手架，共 7 个文件：

```
<项目根>/
├── config/
│   ├── env.yaml                    # 多环境配置（base_url / db / auth / hooks）
│   ├── schemas/
│   │   ├── user.yaml               # 用户实体造数 Schema（示例）
│   │   └── order.yaml              # 订单实体造数 Schema（示例）
│   └── db/
│       └── schema.sql              # 建表脚本，bootstrap 阶段执行
├── cases/
│   ├── api_user_crud.yaml          # 示例用例：用户增删改查 + 接口↔库一致性
│   └── api_user_boundary.yaml      # 示例用例：边界与异常输入变异回归
└── .gitignore                      # 忽略 out/ 与造数登记文件
```

行为约定：

- **不覆盖已有文件**——目标文件已存在时跳过并提示，可安全地重复执行或对半成品目录补齐。
- `--base-url` 与 `--set` 的值会写进 `config/env.yaml` 的 `envs.<环境名>` 段（默认 `local`），与运行时 `--set` 的临时覆盖不同。
- **有守卫**：在 ForgeQA 源码包目录内执行会被拒绝（避免污染源码树），换项目时记得 `cd` 到目标目录或用 `--root`。
- init 只写上面这 7 个文件，不会碰项目里的其他任何文件。

### 不想要了，怎么撤销

init 生成的内容全部在固定路径，删除即可完全还原：

```bash
cd 你的项目目录

# 方式一：整目录删（连同运行产物一起清掉，最常用）
rm -rf config cases out

# 方式二：只删 init 生成的 7 个文件（保留目录里你自己加的东西）
rm -f config/env.yaml config/schemas/user.yaml config/schemas/order.yaml \
      config/db/schema.sql cases/api_user_crud.yaml cases/api_user_boundary.yaml \
      .gitignore
```

> 注意：方式一会连同 `config/schemas/`、`cases/` 里**你自己后来添加的 schema 和用例一起删掉**；
> 如果目录里已有自研内容，用方式二精确删除，再手动清理变空的目录。

`out/`（报告 / 造数数据 / 基线 / 截图）是运行产物而非 init 产物，`forgeqa run` 跑一次就会重新出现，删掉无损。

---

## 程序入口：从哪个文件执行

**执行入口是 `forgeqa/cli.py`**，所有子命令（`init` / `scan` / `import` / `probe` / `gen` / `seed` / `run` / `inventory` / `db` / `demo`）都定义在这里，由 `main()` 统一分发。

三种等价的执行方式：

```bash
# 1) 安装后使用 forgeqa 命令（推荐）
#    pip install -e ".[all]" 时，pyproject.toml 的 [project.scripts] 注册了：
#    forgeqa = "forgeqa.cli:main"
forgeqa run --cases cases

# 2) 不安装，直接以模块方式运行
python -m forgeqa.cli run --cases cases

# 3) 直接执行脚本文件
python forgeqa/cli.py run --cases cases
```

> 用 `forgeqa` 命令依赖安装；后两种方式只要在仓库根目录、依赖装好即可，适合临时调试。
> `cli.py` 本身只做参数解析与分发，实际逻辑分派给 `runner.py`（执行）、`factory.py`（造数）、`scan.py`（扫描）等模块，见[工程结构](#工程结构)。

---

## 适配你自己的网站

### 第 1 步：填配置

只改 `config/env.yaml`：

```yaml
envs:
  staging:
    base_url: https://staging.your-site.com
    db:
      driver: sqlalchemy
      dsn: mysql+pymysql://qa:PASS@10.0.0.5:3306/appdb
    auth:
      type: bearer
      login:
        method: POST
        path: /api/login
        json: {username: "${os:QA_USER}", password: "${os:QA_PASS}"}
        extract: {token: "$.data.token"}
```

### 第 2 步：用真实响应反推造数 schema

```bash
forgeqa probe https://staging.your-site.com/api/users --entity user
# 生成的字段类型是推导出来的，人工核对枚举值 / 长度限制后再用
$EDITOR config/schemas/user.yaml
```

### 第 3 步：写用例（复制改路径即可）

```yaml
id: TC-USER-001
title: 创建用户并校验落库
priority: P0
tags: [api, smoke]
data:
  user: user.yaml
steps:
  - name: 调接口
    http: {method: POST, path: /api/users, json: {name: "${data.user.name}"}}
    extract: {uid: "$.data.id"}
    assert:
      - {status: 201}
      - {jsonpath: "$.data.name", op: eq, value: "${data.user.name}"}
  - name: 查库
    db:
      sql: "SELECT name FROM users WHERE id = :uid"
      params: {uid: "${ctx.uid}"}
    assert:
      - {rows_count: 1}
      - {sql: "SELECT COUNT(*) FROM users WHERE email = :e",
         params: {e: "${data.user.email}"}, op: eq, value: 1,
         label: 邮箱唯一性业务不变量}
```

### 第 4 步：UI 站点加多策略选择器

```yaml
  - name: 提交表单
    ui:
      - {action: goto, url: /signup}
      - {action: fill, target: {label: 姓名, placeholder: 请输入姓名, css: "#name"},
         value: "${data.user.name}"}
      - {action: click, target: {text: 提交, css: "#submitBtn"}}
    assert:
      - {visible: {text: 提交成功}}
      - {screenshot_diff: {name: signup_success, threshold: 0.05}}
```

`target` 给多个候选定位策略，**按顺序尝试，谁先命中用谁**，首选策略等满超时、备选策略快速试探。
页面结构微调时通常不需要改用例——这是 UI 层「适配任意网站」的落地方式。

---

## 扫描站点：自动发现接口并生成用例

懒得逐个接口写 probe？`scan` 命令从一个入口 URL 出发自动发现接口，一键生成用例草稿：

```bash
forgeqa scan http://your-site.com          # 扫描 + 生成 schema + 生成冒烟用例
forgeqa scan http://your-site.com --print-only   # 先只看看，不写文件
```

扫描走三路（质量从高到低）：

| 来源 | 做法 | 可靠度 |
|---|---|---|
| OpenAPI 文档 | 探测 `/openapi.json`、`/swagger.json`、`/v3/api-docs` 等 | 接口清单精确 |
| 页面爬取 | 抓 HTML 链接/表单 + 内联 JS 里的 `/api/...` 字符串，只爬同源、限页数 | 看前端写没写 |
| 路径字典 | `/api/users`、`/health` 等高频路径逐个 GET 试探，405+`Allow` 也能发现非 GET 接口 | 盲区最大 |

产出两样东西：

- `config/schemas/<entity>.yaml` —— 从 JSON 响应**反推的造数 Schema**（枚举值、长度等业务约束需人工核对；目标文件已存在时不覆盖，写 `.inferred.yaml` 备份）
- `cases/_generated/_scan_<host>.yaml` —— **可直接运行的 GET 冒烟用例** + 注释形式的 POST 草稿提示。`_` 前缀保证默认 `--cases cases` 不会误跑草稿，显式指定即可执行

完整接入流程（换新站点时）：

```bash
# 1. 扫描：发现接口、反推 schema、生成冒烟
forgeqa scan http://your-site.com

# 2. 核对反推的 schema（重点看枚举、长度、必填），然后跑生成的冒烟
forgeqa run --cases cases/_generated/_scan_<host>.yaml

# 3. 参考 cases/api_user_crud.yaml，把核心 POST/SQL/UI 场景补成正式用例
#    （scan 的输出里已列出发现的 POST 接口和对应 schema 路径作为提示）
```

已知边界：**登录墙后的接口扫不到**（先 `export FORGEQA_TOKEN=<token>` 重扫）；
纯前端 SPA 的接口若既不在 HTML 也不在 JS 字符串里，只能靠 OpenAPI 文档或手工补充；
扫描只能发现「接口存在」，业务规则（什么算对）永远需要人来定义。

---

## 导入接口文档：从 OpenAPI/Swagger 生成 POST 用例

`scan` 在线探测的短板是**写接口**（POST/PUT/PATCH/DELETE）：请求体结构探测猜不出来，
所以只能留草稿提示。但如果你手里有**接口文档**（OpenAPI 3 / Swagger 2，JSON 或 YAML，
本地文件或 URL 均可），请求体 Schema 就写在文档里——`import` 命令把它翻译成
造数 Schema 和可直接运行的 POST 用例：

```bash
forgeqa import ./openapi.yaml                      # 从本地文档导入
forgeqa import http://your-site.com/openapi.json   # 从 URL 导入
```

> Apifox / Postman 等工具都可以把项目导出为 OpenAPI 格式后导入；Postman Collection 原生格式暂不支持。

产出两样东西：

- `config/schemas/<entity>.yaml` —— 从 requestBody Schema 翻译的造数 Schema：
  `enum` → 加权选择、`format: email/uuid/date` 与字段名语义（phone/name/city…）→ 对应生成器、
  `minLength/maxLength` → `min_len/max_len`（同时驱动变异造数）、`integer/number/boolean` → 区间/概率生成
- `cases/_generated/_import_<名称>.yaml` —— 三类内容：
  1. **POST 正常路径用例**（可直接运行，断言「不出现 5xx」）
  2. **POST 边界与异常变异用例**（min-1/max+1/SQLi/XSS 等逐条打接口）
  3. GET 冒烟 + 注释形式的带路径参数接口草稿（如 `PUT /api/users/{id}`）

完整流程（拿到接口文档时）：

```bash
# 1. 导入：生成 schema 与 POST 用例草稿
forgeqa import ./openapi.yaml --name myproject

# 2. 人工核对 config/schemas/*.yaml 的枚举含义、必填语义、长度上限，
#    并给唯一字段（用户名/邮箱）加 transform: "suffix:${uniq}" 防撞车

# 3. 运行生成的草稿
forgeqa run --cases cases/_generated/_import_myproject.yaml

# 4. 把通过的草稿转正：去掉文件名的 _ 前缀、按业务补 SQL/UI 断言后移入 cases/
```

翻译约定与边界：

| 文档里的定义 | 生成结果 |
|---|---|
| `enum: [user, admin]` | `gen: choice` |
| `format: email` / 字段名含 phone、name、city… | 对应 Faker provider / 脱敏号段 |
| `minLength` / `maxLength` | `min_len` / `max_len`（驱动边界变异） |
| `type: integer`（名含 id） | `gen: seq` 自增，其余 `gen: int` 区间 |
| 嵌套 `object` / `array` | `gen: const` 占位（引擎暂不支持嵌套生成，需人工补全） |
| 带路径参数的写接口 `/api/users/{id}` | 不生成可执行用例（需先造资源），以注释草稿列出 |

已知边界：文档里的业务约束机器读不全（哪些枚举值在什么条件下合法等），产出全部定位为**草稿**，
`_` 前缀保证默认 `--cases cases` 不会误跑，人工核对转正后才有门禁效力。

---

## 造数：Schema 参考

一份 schema = 一个实体。文件放 `config/schemas/<entity>.yaml`。

```yaml
entity: user
count: 1
unique: [email, phone, username]      # 批次内去重（200 次重试上限）
fields:
  - {name: name,       gen: faker, method: name, min_len: 2, max_len: 20}
  - {name: username,   gen: pattern, pattern: "qa_????", transform: "suffix:${uniq}"}
  - {name: phone,      gen: fake_phone}
  - {name: age,        gen: int, min: 18, max: 65}
  - {name: role,       gen: choice, values: [user, admin], weights: [9, 1]}
  - {name: vip,        gen: expr, value: "${age} >= 30"}
  - {name: created_at, gen: datetime, start: "-30d", end: now, fmt: "%Y-%m-%d %H:%M:%S"}
```

### `gen` 生成器

| gen | 参数 | 说明 |
|---|---|---|
| `faker` / `fake` | `method`, `args` | 调用 Faker provider，如 `name` / `email` / `company` / `address` |
| `const` | `value` | 固定值（支持 `${}` 插值） |
| `seq` | `start`, `step`, `width`, `prefix` | 自增序列 |
| `int` / `float` | `min`, `max`, `precision` | 区间随机 |
| `bool` | `p` | 概率为真 |
| `choice` / `enum` | `values`, `weights` | 加权随机选择 |
| `pattern` | `pattern` | `?`=大写字母 `#`=数字 `@`=小写字母，其余为字面量 |
| `datetime` | `start`, `end`, `fmt` | 支持 `now` / `-30d` / `-2h` / `2026-01-01` |
| `regex` | `pattern` | 正则子集展开（字符类、`\d`、`{n,m}`、`+*?`） |
| `uuid` | — | UUID4 |
| `expr` | `value` | 派生字段，引用其他字段写 `${字段名}`，如 `${age} >= 30` |
| `ref` | `entity`, `field` | 引用已生成实体的字段值（做外键） |
| `list` | `count`, `item` | 嵌套数组 |
| `fake_phone` / `fake_id_card` | — | 明显虚构的脱敏号段（`1380000xxxx`） |

### `transform` 字段后处理

`transform: "name"` 或带参数 `"truncate:8"`，多个用 `|` 串联 `"strip|lower"`：

`fake_phone` `fake_id_card` `fake_bank_card` `fake_company` `lower` `upper` `strip` `title`
`truncate:N` `prefix:X` `suffix:X` `mask:首:尾` `md5` `sha1` `b64` `to_str` `to_int` `to_float`
`default_if_blank:X` `json` `dump`

### `min_len` / `max_len` 不只是文档

它会驱动变异造数自动生成**长度边界**用例（min-1、min、max、max+1）。

### 变异造数

```bash
forgeqa gen --schema config/schemas/user.yaml --mutate --save
```

输出结构（可直接喂给 pytest 参数化）：

```json
{
  "case_id": "USER-MUT-003",
  "entity": "user",
  "target": "name",
  "category": "abnormal",
  "description": "name=sqli",
  "data": {"name": "'; DROP TABLE users;--", "age": 47, ...}
}
```

三个类别的覆盖范围：

| category | 覆盖内容 |
|---|---|
| `boundary` | min-1 / min / max / max+1、长度边界、允许为空的字段缺省 |
| `abnormal` | 必填缺省、类型混淆（数字传字符串、整数传小数）、空串、纯空格、`null`/`undefined` 字面量、XSS、SQL 注入（含 UNION）、路径穿越、空字节、CRLF、模板注入 |
| `extreme` | ±10^18、64KB 长串、emoji 轰炸、深层嵌套 JSON、空数组 |

### 脱敏约定

**造数永不生成真实个人信息。** 手机号固定 `1380000xxxx` 号段，身份证固定 `11010119900101xxxx`
（校验位故意不合法）。代码里用常量 `FAKE_PHONE_PREFIX` / `FAKE_ID_PREFIX` 控制，便于审计。

---

## 用例：YAML 参考

```yaml
id: TC-XXX-001                 # 必填
title: 用例标题
priority: P0                   # P0/P1/P2/P3，决定执行顺序
layer: api                     # api / db / ui，缺省按步骤推断
tags: [api, smoke]             # 用于 --tags / --exclude-tags 筛选
retries: 2                     # 用例级重试（只对环境类异常生效）
skip: "环境未就绪"              # 跳过并记录原因

data:                          # 造数
  user: user.yaml                                      # 引用 schema 文件
  users: {schema: user.yaml, count: 5}                 # 带参数
  items: [1, 2, 3]                                      # 字面量，直接可用
  muts: {schema: user.yaml, mutate: true, categories: [boundary, abnormal]}
  inline: {entity: t, fields: [{name: v, gen: int, min: 1, max: 9}]}   # 内联

setup: []                      # 与 steps 同构，先执行
teardown: []                   # 用例结束必执行（即使中途失败）

steps:
  - name: 步骤名
    on_fail: continue          # abort（默认）| continue —— 注意：continue 不改变用例最终状态
    if: "${ctx.flag}"          # 条件执行，支持表达式
    # ↓ 五选一（或只写 assert 做纯断言步骤）
    http: {...}
    db: {...}
    ui: [...]
    script: {...}
    sleep: 0.5
    log: "变量当前值 ${ctx.uid}"
    loop: {over: "${data.users}", as: u, steps: [...], on_fail: abort}
    extract: {变量名: 提取规则}
    assert: [...]
```

### `http` 步骤

```yaml
http:
  method: POST                  # GET/POST/PUT/PATCH/DELETE
  path: /api/users              # 相对 base_url；写完整 URL 则直接使用
  params: {page: 1}             # query string
  json: {...}                   # JSON body（自动 Content-Type）
  data: {...}                   # 表单 body
  headers: {X-Trace: "${uuid}"}
  cookies: {...}
  timeout: 10
  retries: 0                    # 覆盖全局重试
  auth: none                    # 步骤级覆盖鉴权（测未授权场景）
  raw_url: false
extract:
  uid: "$.data.id"                                   # JSONPath
  sid: {header: Set-Cookie, regex: "session=(\\w+)"}
  cnt: {jsonpath: "$.data.total", cast: int, default: 0}
```

### `db` 步骤

```yaml
db:
  mode: query                   # query（默认）| execute | script | file
  sql: "SELECT * FROM users WHERE id = :uid"
  params: {uid: "${ctx.uid}"}   # 具名参数（:name），自动过滤 SQL 里未用到的键
extract:
  uid: "rows.0.id"              # 也支持 count / scalar / rows / {field, index} / {sql: ...}
baseline:                       # 快照回归：与基线 diff
  table: users
  key: id
  fields: [name, status]
  where: "status = 1"
```

### `ui` 步骤

```yaml
ui:
  - {action: goto, url: /form}
  - {action: fill, target: "#name", value: "${data.user.name}"}
  - {action: click, target: {text: 提交, css: "#submitBtn"}}
  - {action: select, target: "#dept", value: 测试部}
  - {action: wait_for, target: "#msg.ok"}
  - {action: frame, target: "iframe#content"}       # 进入 iframe；frame: null 回主文档
  - {action: block, url: "**/analytics/**"}         # 屏蔽埋点，降低噪声
  - {action: mock, url: "**/api/rate", mock: {code: 0}}   # 接口 mock
  - {action: exec_js, script: "return document.title", var: page_title}
  - {action: cookie, var: session_cookie}            # UI 登录态 → 注入后续接口请求
  - {action: screenshot, name: after_submit, full_page: true}
```

完整动作清单：`goto` `reload` `go_back` `go_forward` `click` `dblclick` `fill` `type` `press`
`hover` `check` `uncheck` `select` `upload` `drag` `scroll` `focus` `clear` `wait_for` `sleep`
`frame` `exec_js` `screenshot` `cookie` `block` `route_free`

选择器策略（`target`）：

```yaml
target: {label: 姓名, placeholder: 请输入姓名, css: "#name", fallback: ["[name=name]"]}
target: "text=登录"       # Playwright 原生语法
target: "#submit"         # CSS 简写
target: "//button"        # XPath
```

### `script` 步骤（逃生舱）

```yaml
# 表达式求值：${} 先替换成字面量，再当 Python 表达式算
script: {expr: "${ctx.before} + 1", var: expected_count}

# 项目内脚本：文件里写 def run(ctx, args) -> dict
script: {file: scripts/gen_order_no.py, args: {prefix: QA}, var: order_no}

# 受限沙箱内的一行代码
script: {code: "len(ctx.layers['data']['users_list'])", var: n}
```

---

## 配置：env.yaml 参考

```yaml
default_env: local

defaults:                        # 所有环境共享，各环境只写差异
  http:
    timeout: 15
    retries: 2
    backoff: 0.4
    verify_ssl: true
    trust_env: true              # 是否走环境代理
    no_proxy: [localhost, 127.0.0.1, "10.*", "192.168.*"]   # 命中则直连
    headers: {User-Agent: forgeqa/1.0}
  ui: {browser: chromium, headless: true, timeout: 15000,
       viewport: {width: 1440, height: 900}, screenshot_on_fail: true,
       fallback_probe_timeout: 1200}
  db:
    driver: sqlite               # sqlite（零依赖）| sqlalchemy
    path: ./out/forgeqa.db
    # dsn: mysql+pymysql://qa:pass@host:3306/db
  auth:
    type: bearer                 # none | bearer | basic | header | api_key | cookie | login
    login:
      method: POST
      path: /api/login
      json: {username: "${os:QA_USER:-admin}", password: "${os:QA_PASS:-admin123}"}
      extract: {token: "$.data.token"}

generators:
  locale: zh_CN
  seed: 20260922                 # 固定 → 造数可复现；CI 里建议固定
  out_dir: ./out/data

runner: {retries: 0, repeat: 1, jobs: 1, fail_fast: false}
report: {out_dir: ./out/reports, junit: true}

hooks:
  ddl: config/db/schema.sql      # bootstrap 阶段执行建表
  seed:                          # 造数入库（会登记，跑完精确回收）
    - {entity: dept, schema: dept.yaml, count: 3, table: dept,
       mapping: {dept_no: dept_no}, defaults: {status: 1}}
  cleanup: true
  bootstrap: []                  # 自定义准备步骤（与用例步骤同构）
  post: []                       # 收尾步骤

envs:
  local:   {base_url: http://127.0.0.1:8000}
  staging: {base_url: https://staging.example.com, db: {driver: sqlalchemy, dsn: ...}}
```

### CI 覆盖

配置支持环境变量覆盖，便于流水线注入：

`FORGEQA_BASE_URL` · `FORGEQA_ENV` · `FORGEQA_DB_PATH` · `FORGEQA_DB_DSN` ·
`FORGEQA_DB_DRIVER` · `FORGEQA_UI_HEADLESS` · `FORGEQA_UI_BROWSER`

命令行临时覆盖（优先级最高）：

```bash
forgeqa run --set base_url=https://ci.example.com --set db.path=./out/ci.db
```

`--set` 支持三种写法，按需选用：

| 写法 | 作用 | 示例 |
|---|---|---|
| `配置段.键=值` | 覆盖 `defaults` 段里的配置 | `--set http.retries=0`、`--set ui.headless=false` |
| `裸键=值` | 覆盖当前环境的键 | `--set base_url=http://127.0.0.1:8020` |
| `envs.环境名.键=值` | 覆盖指定环境的键（不影响当前环境） | `--set envs.staging.base_url=https://stg.x` |

`env.` 与 `envs.` 同义，写 `--set env.local.base_url=...` 也认。

> 临时换一套环境跑同一批用例，用 `--set` 就够了，不必改配置文件：
> ```bash
> forgeqa run --cases cases \
>   --set base_url=http://127.0.0.1:8020 \
>   --set db.path=/tmp/e2e.db
> ```

---

## 变量与模板语法

任何 YAML 值里都能插值。**整串就是一个表达式时保留原始类型**（int / dict / list / bool 不会被转成字符串）。

| 写法 | 含义 |
|---|---|
| `${env.base_url}` | 配置/上下文点号路径取值 |
| `${data.user.email}` | 数据工厂生成的字段 |
| `${ctx.uid}` | 运行时变量（extract / script 产出） |
| `${cfg.generators.seed}` | 原始配置树 |
| `${os:HOME}` / `${os:FOO:-默认}` | 环境变量（带默认值） |
| `${uniq}` / `${uniq:6}` | **用例级唯一标记**：同一用例恒定，跨用例、跨运行不同 |
| `${now}` `${now:%Y%m%d}` `${today}` | 时间 |
| `${ts}` `${ts:3600}` | 时间戳（可加偏移秒） |
| `${uuid}` | UUID4 |
| `${faker:name}` `${faker:random_int:1:100}` | Faker |
| `${randint:1:100}` `${randfloat:0:1:2}` | 随机数 |
| `${choice:a\|b\|c}` | 随机选择 |
| `${seq:order_no:1000:1}` | 自增序列（名:起始:步长） |
| `${md5:x}` `${sha1:x}` `${b64:x}` `${upper:x}` `${lower:x}` | 编码/哈希 |
| `${不存在的变量:-兜底值}` | 默认值语法 |

> **注意**：`${...}` 出现在 `{ }` 流式写法里必须加引号：
> `{value: "${data.user.age}"}` ✅　`{value: ${data.user.age}}` ❌（YAML 语法错误）

**关于 `${uniq}`**：它刻意**不受 seed 影响**。职责是让每次运行的数据互不撞车——
业务表上有唯一约束的字段（用户名、邮箱、工单号）务必带上它，否则两条用例会撞出 409，
看起来像缺陷，其实是测试数据串扰。

---

## 断言算子清单

35 个算子，接口 / SQL / UI 三层共用：

`eq` `ne` `gt` `gte` `lt` `lte` `contains` `not_contains` `in` `not_in` `regex`
`startswith` `endswith` `is_null` `not_null` `is_true` `is_false` `len_eq` `len_gte` `len_lte`
`between` `empty` `not_empty` `type_is` `absent` `present` `approx` `deep_eq` `matches_schema`

**宽松相等**：`"1"` 与 `1`、`1.0` 与 `1` 视为相等（接口返回字符串数字是常态）。
`type_is: int` 不会把 `true` 当整数。

### JSONPath 子集

自研实现，不引入额外依赖：

```yaml
$.data.id                 # 取字段
$.data.list[0].name       # 数组下标（支持 -1）
$.data.list[*].id         # 展开数组
$..id                     # 递归下降
$.data.list[?(@.age>30)]  # 过滤（支持 == != > >= < <=）
$.data.meta.*             # 对象所有值
$['data']['total']        # 引号键
```

### 结构校验

```yaml
assert:
  - schema:
      type: object
      required: [code, data]
      properties:
        code: {type: integer, min: 0, max: 0}
        data:
          type: object
          required: [id, name]
          properties:
            id: {type: integer}
            name: {type: string, min_len: 1, max_len: 20, pattern: "^[\\u4e00-\\u9fa5]+$"}
            tags: {type: array, min_items: 0, items: {type: string}}
```

---

## 断言方法清单

### 接口断言

```yaml
assert:
  - {status: 201}                          # 单值 / 列表 [200,201] / 区间 {min: 200, max: 299}
  - {jsonpath: "$.data.id", op: not_null, label: 返回了主键}
  - {jsonpath: "$.data.name", op: eq, value: "${data.user.name}"}
  - {jsonpath: "$.list[*].status", op: all, value: active}     # 每个元素都满足
  - {jsonpath: "$.list[*].id", op: count, value: 10}
  - {schema: {...}}
  - {header: Content-Type, op: contains, value: application/json}
  - {text_contains: "success"}
  - {time_lt: 3000}                        # 响应时间（毫秒）
  - {body_len_gt: 10}
```

### SQL 断言

```yaml
assert:
  - {rows_count: 1}                        # 也支持 {op: gte, value: 1}
  - {row: {field: name, op: eq, value: "${data.user.name}"}}
  - {each_row: {field: status, op: eq, value: 1}}
  - {scalar: {op: gt, value: 0}}
  - {not_empty: true}
  - {table_exists: users}
  # 业务不变量（最推荐）：直接写一条 SQL 取标量来比
  - {sql: "SELECT COUNT(*) FROM users WHERE email = :e",
     params: {e: "${data.user.email}"}, op: eq, value: 1, label: 邮箱唯一}
  # 快照回归
  - {baseline: {table: users, key: id, fields: [name, status]}}
```

### UI 断言

```yaml
assert:
  - {visible: {text: 提交成功}}      - {hidden: "#loading"}
  - {text_contains: {target: "#msg", value: 成功}}
  - {text_equals: {target: "#msg", value: 提交成功}}
  - {value: {target: "#email", value: "a@b.com"}}
  - {count: {target: "tbody tr", op: gte, value: 3}}
  - {url_contains: /success}         - {title_contains: 首页}
  - {enabled: "#submit"}             - {disabled: "#submit"}
  - {checked: "#agree"}
  - {attribute: {target: "#a", name: href, op: contains, value: /detail}}
  - {screenshot_diff: {name: home, threshold: 0.02}}    # 像素级视觉回归
```

---

## 命令行参考

```
forgeqa init        生成项目脚手架（config / schemas / cases / ddl）
forgeqa scan        扫描站点发现接口，生成冒烟用例草稿与造数 schema
forgeqa import      从 OpenAPI/Swagger 接口文档生成 POST 用例草稿与造数 schema
forgeqa probe       探测接口，从真实响应反推造数 schema
forgeqa gen         生成数据集（正常 + 边界/异常/极端变异）
forgeqa seed        按计划造数入库（可精确回收）
forgeqa run         执行回归 + 生成报告
forgeqa inventory   用例盘点与覆盖度自查
forgeqa db          init / script / tables / query / snapshot / cleanup
forgeqa demo        启动内置演示站点
```

常用组合：

```bash
# 只跑 P0 冒烟集，快
forgeqa run --cases cases --priority P0 --tags smoke

# 稳定性探测：重复 5 轮，识别 flaky
forgeqa run --cases cases --repeat 5 --retries 1 --fail-on-flaky

# 基线回归：结构或数据变了就报出来
forgeqa run --cases cases --baseline diff

# 单条用例调试
forgeqa run --cases cases -k 订单 --verbose --verbose

# 查看/清理造数
forgeqa db tables
forgeqa db query --sql "SELECT id,name FROM users LIMIT 5"
forgeqa db snapshot --table users --key id --diff
forgeqa db cleanup

# 并发
forgeqa run --cases cases --jobs 4
```

**退出码**：`0` 全部通过 · `1` 存在失败或异常 · `2` 配置/用法错误 · `3` 存在 flaky（配合 `--fail-on-flaky`）

---

## 接入 CI

本仓库已自带可用的流水线 [`.github/workflows/regression.yml`](.github/workflows/regression.yml)：
push / PR 自动触发，Python 3.10 / 3.12 / 3.13 三档矩阵跑全量单测，随后用内置演示站点
跑「造数 → 接口 → 查库 → UI → 断言 → 清理」端到端闭环，失败仍上传 HTML 报告与日志。
推送到 GitHub 后即可在 Actions 页看到结果，无需任何配置。

接入自己的被测系统时，把「启动演示站点」一步换成拉起你的服务，其余不变：

```yaml
# .github/workflows/regression.yml
name: regression
on: [push, pull_request]

jobs:
  forgeqa:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}

      - run: pip install -e ".[all]" && playwright install --with-deps chromium
      - run: docker compose up -d app          # 拉起被测系统

      - name: 回归
        env:
          FORGEQA_ENV: staging
          FORGEQA_BASE_URL: http://localhost:8080
          QA_USER: ${{ secrets.QA_USER }}
          QA_PASS: ${{ secrets.QA_PASS }}
        run: forgeqa run --cases cases --baseline diff --fail-on-flaky

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: forgeqa-report
          path: out/reports/
```

`out/reports/junit-*.xml` 是标准 JUnit 格式，Jenkins / GitLab / 云效可直接解析。

**建议的门禁策略**

| 阶段 | 集合 | 要求 |
|---|---|---|
| 提交前 | `--tags smoke`（P0） | 必须 100% 通过，零 flaky |
| 合并前 | 全量 + `--baseline diff` | 失败阻塞合并 |
| 每日 | 全量 + `--repeat 3` | 统计 flaky 率，超 1% 先修稳定性再扩量 |

---

## 设计取舍

**为什么 SQLite 是默认数据库？**
零依赖、零服务、CI 里开箱即用。生产同构走 `driver: sqlalchemy`，SQL 语句和参数风格完全一致（统一 `:name`）。

**为什么断言失败不重试？**
重试只对「环境类异常」生效（超时、连接失败、5xx、库锁）。断言失败重试等于掩盖缺陷——
重试后通过的用例会被标记 `flaky` 并单列出来，因为它掩盖的问题迟早要还。

**为什么自研 JSONPath？**
依赖越少越稳，且实战子集覆盖 95% 场景。语法错误直接给出正确写法的提示。

**`on_fail: continue` 的语义**
只表示「失败后继续跑后续步骤」（用于一次跑完拿到全景），**不改变用例最终状态**。否则 CI 门禁会漏放。

**失败根因四分类**
`FAILED`（断言未通过）优先怀疑被测系统；`ERROR`（工具层异常）优先怀疑脚本/环境/数据。
报告里再按关键词进一步分成 缺陷 / 环境 / 脚本 / 数据 四类，作为**初判**辅助人工定位，不做终审。

**测试数据不留痕**
三层回收：用例内 `teardown` 步骤 → seeding 登记精确删除 → `forgeqa db cleanup` 兜底。
演示套件跑完，`dept` / `users` / `orders` 三张表都是 0 行。

---

## 工程结构

完整文件树（行数为实际代码行）：

```
forgeqa/
├── .github/workflows/
│   └── regression.yml        (104)  CI 流水线：Python 3.10/3.12/3.13 单测矩阵 + 演示站点端到端
│
├── forgeqa/                          # 核心包 —— 换站点零改动
│   ├── __init__.py            ( 31)  包导出
│   ├── cli.py                (1082)  ★ 命令行入口（forgeqa 命令的执行入口）：init / scan / import / probe / gen / seed / run / inventory / db / demo
│   ├── runner.py             (1257)  ★ 用例引擎：任务调度、变量传递、失败分拣、重试、并发——全工具的心脏
│   ├── scan.py                (373)  站点扫描：OpenAPI 探测 / 页面爬取 / 路径字典，自动生成冒烟用例草稿
│   ├── apidoc.py              (416)  接口文档导入：OpenAPI/Swagger → 写接口（POST）用例草稿与造数 Schema
│   ├── config.py              (549)  多环境配置 + ${} 模板引擎 + 变量池 + raw←env←overrides 三层合并
│   ├── factory.py             (788)  造数引擎：Faker / 派生字段 / 边界·异常·极端变异 / schema 反推
│   ├── httpclient.py          (571)  requests 封装：变量提取、重试退避、基线录制、代理绕过
│   ├── db.py                  (561)  SQL 层：SQLite 零依赖 / SQLAlchemy 双驱动、精确回收、快照 diff
│   ├── uiauto.py              (611)  Playwright 声明式 DSL：多策略选择器、视觉像素回归
│   ├── assertions.py          (412)  35+ 断言算子 + 自研 JSONPath 子集 + 结构校验（接口/SQL/UI 共用）
│   ├── report.py              (420)  自包含 HTML 报告（截图内嵌）+ JUnit XML + 失败根因四分类
│   └── errors.py              ( 64)  统一异常体系，每个异常自带 hint 修复建议
│
├── config/                           # ★ 换站点主要改这里
│   ├── env.yaml               ( 71)  多环境定义：base_url / db / auth / seeding 计划 / DDL 路径
│   ├── schemas/                      # 造数 Schema（字段来源、派生、唯一标记 ${uniq}）
│   │   ├── user.yaml          ( 23)
│   │   ├── order.yaml         ( 10)
│   │   └── dept.yaml          (  9)
│   └── db/
│       └── schema.sql         ( 34)  建表脚本，bootstrap 阶段无条件执行
│
├── cases/                            # ★ 用例层——一条 YAML = 一条全链路回归
│   ├── api_user_crud.yaml     ( 55)  用户增删改查 + 接口↔库一致性
│   ├── api_order_invariant.yaml (98)  下单业务不变量（余额/库存联动）
│   ├── api_user_boundary.yaml ( 43)  边界与异常输入（超长/空值/非法格式）
│   ├── db_consistency.yaml    ( 54)  纯 SQL 层：快照 diff 与精确回收验证
│   └── ui_user_form.yaml      ( 64)  UI 表单：多策略选择器 + 截图
│
├── examples/
│   ├── demo_server.py         (484)  内置 Flask 演示站点（真实校验逻辑：唯一约束、业务不变量）
│   └── selfcheck_cases/
│       └── selfcheck_must_fail.yaml (34)  故意失败的用例，验证工具能抓出问题
│
├── tests/                            # 277 个单元测试，按模块拆分
│   ├── test_runner.py         (419)  用例引擎端到端流程
│   ├── test_config.py         (284)  配置三层合并、插值、循环引用守卫
│   ├── test_factory.py        (242)  造数可复现性与变异
│   ├── test_db.py             (186)  SQL 层与回收
│   ├── test_assertions.py     (173)  断言算子与 JSONPath
│   ├── test_scan.py           (183)  站点扫描与用例草稿生成
│   ├── test_apidoc.py         (322)  接口文档导入：Schema 翻译、用例生成、端到端
│   └── test_cli.py            (130)  --set 参数映射与优先级、init 守卫
│
├── out/                              # 运行产物（报告/数据/基线/截图），已 gitignore，跑一次就有
├── pyproject.toml                    # 包元数据 + 依赖分组 + forgeqa 命令入口
├── requirements.txt                  # 运行依赖（锁到实测版本）
├── requirements-dev.txt              # + pytest / pytest-cov
├── requirements.lock.txt             # 含传递依赖的完整闭包（28 包），换机复现用
├── LICENSE                           # MIT
└── README.md
```

**一次运行时，模块间这样协作**（`forgeqa run` 之后）：

```
cli.py ──▶ runner.py（引擎）
             │
             ├─ config.py     读配置、插值 ${}、三层合并
             ├─ factory.py    造数（含 ${uniq} 隔离标记）──▶ db.py 入库
             ├─ httpclient.py 调接口、提取变量 ──▶ 断言
             ├─ uiauto.py     需要时驱动浏览器、截图
             ├─ db.py         SQL 校验 / 快照 diff
             ├─ assertions.py 接口·SQL·UI 三层共用同一套算子
             └─ report.py     汇总结果 ──▶ HTML + JUnit XML + 根因分类
```

**换站点只动三处**：`config/env.yaml`（地址/库/登录）、`config/schemas/*.yaml`（造数规则）、`cases/*.yaml`（用例）。`forgeqa/` 包内代码零改动——这是整个设计的核心承诺。

**每一层都遵循同一个约定**：报错必须带 `hint`——可执行的修复建议，而不是让人去猜。
例如：

```
✗ 变量 'ctx.greeting' 未定义
  → 修复建议: 检查拼写；造数变量来自数据工厂，运行时变量来自 extract 或 setup 步骤
```

---

## 测试

```bash
PYTHONPATH=. pytest tests -q
```

**277 个用例**，全部通过。分布：

| 文件 | 用例数 | 覆盖内容 |
|---|---|---|
| `test_assertions.py` | 65 | 断言算子、JSONPath 子集、结构校验 |
| `test_cli.py` | 17 | `--set` 参数映射与优先级、init 守卫 |
| `test_config.py` | 39 | 配置三层合并、变量插值、循环引用守卫 |
| `test_db.py` | 22 | SQL 层、造数回收、快照 diff |
| `test_factory.py` | 38 | 造数可复现性、变异、schema 反推 |
| `test_runner.py` | 39 | 用例引擎端到端流程 |
| `test_scan.py` | 32 | 站点扫描、schema 反推写入、用例草稿生成 |
| `test_apidoc.py` | 25 | 文档加载、$ref 解析、Schema 翻译、导入端到端 |

不依赖网络与外部服务（SQLite + 合成响应）。
