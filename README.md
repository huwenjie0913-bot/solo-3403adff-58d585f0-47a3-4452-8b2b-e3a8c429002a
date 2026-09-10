# 字幕断行与时间轴校正 API

供字幕制作团队校正影视字幕断行和时间轴的 REST API。接收 SRT / WebVTT 字幕、
帧率、镜头切点和一套可配置规范，逐条检测问题并给出原因与修正候选；自动修复
接口会结合标点、语句长度和可用时间窗重新断句、分行并调整起止时间。

**全部处理在本地完成，不依赖任何外部模型或服务。**

技术栈：Python 3.11+ · FastAPI · Pydantic v2 · SQLAlchemy 2 · SQLite

## 快速开始

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后访问交互式文档：<http://127.0.0.1:8000/docs>

数据库默认使用当前目录下的 `subtitle_qc.db`，可用环境变量覆盖：

```bash
export SUBTITLE_QC_DB="sqlite:////path/to/my.db"
```

运行测试：

```bash
pip install pytest httpx
python3 -m pytest tests/ -q
```

## 可配置规范（Rules）

| 字段 | 默认 | 说明 |
|---|---|---|
| `max_cps` | 20.0 | 每秒最大字符数（阅读速度上限，按去标签去空白字符计） |
| `max_chars_per_line` | 40 | 单行最大字符数 |
| `max_lines` | 2 | 单条字幕最大行数 |
| `min_duration_ms` | 1000 | 最短显示时间（低于即为“闪现”） |
| `max_duration_ms` | 7000 | 最长显示时间 |
| `min_gap_ms` | 100 | 相邻字幕最小间隔 |
| `max_offset_ms` | 500 | 自动修复允许的单点时间偏移上限 |
| `shot_tolerance_ms` | 0 | 镜头切点容差，容差范围内不算跨镜头 |

规则优先级：项目内联规则 > 规则模板 > 默认值。项目创建时保存规则快照，
之后修改模板不影响已有项目。

## 检测项

| 类型 | 级别 | 说明 |
|---|---|---|
| `overlap` | error | 与后一条字幕时间重叠 |
| `gap_too_small` | warning | 字幕间隔小于最小间隔 |
| `flash` | error | 显示时间短于下限（闪现） |
| `duration_too_long` | warning | 显示时间超过上限 |
| `cps_exceeded` | error | 阅读速度（字/秒）超限 |
| `shot_cross` | error | 字幕跨越镜头切点 |
| `line_too_long` | error | 单行字数超限 |
| `too_many_lines` | error | 行数超限 |
| `break_punct_start` | warning | 行首为闭合标点 |
| `break_open_punct_end` | warning | 行尾为开引号/开括号 |
| `break_func_word` | warning | 行尾为中文虚词（的、了、和…） |
| `break_word_split` | warning | 英文单词在换行处被截断 |
| `break_unbalanced` | warning | 两行长度悬殊 |
| `break_orphan` | warning | 尾行过短、孤字成行 |

每项问题都返回 `message`（原因）和 `fix_candidates`（修正候选，含具体参数）。

## 自动修复策略

1. **时间窗**：每条字幕的可用窗口由 `max_offset_ms`、相邻字幕位置
   （保留 `min_gap_ms`）和镜头切点共同限定；
2. **整条放置**：优先贴近原始起止时间，必要时在窗口内延长/前移以满足
   阅读速度与最短显示时间；
3. **拆分**：窗口放不下或超过最长显示时间时，按 句末标点 → 从句标点 →
   原子硬拆 的顺序拆分，按可见字数比例分配时间；说话人标签（`<v 张三>`）
   与样式标签（`<i>` 等）在拆分后的各段保持闭合并保留（破折号式标签
   `- 张三：` 保留在首段）；
4. **重新分行**：按单行上限贪心分行，遵循避头尾（闭合标点不置行首、
   开引号/开括号不置行尾），两行时自动均衡长度；
5. **帧对齐**：所有调整后的时间点按项目帧率对齐（起点向上、终点向下取整，
   且不侵蚀最小间隔）；
6. **冲突**：以上约束无法同时满足时，该条字幕**保留原稿**，并作为冲突项
   随原因返回；修复结果保存为**新版本**，绝不覆盖原稿。

## API 一览

### 规则模板

```
POST   /rule-templates            创建（name 唯一，重名返回 409）
GET    /rule-templates            列表
GET    /rule-templates/{id}       详情
PUT    /rule-templates/{id}       更新
DELETE /rule-templates/{id}       删除（项目快照不受影响）
```

### 项目（帧率 + 镜头切点 + 规则）

```
POST   /projects                  创建；shot_cuts 数字按毫秒，字符串支持 "HH:MM:SS.mmm"
GET    /projects                  列表
GET    /projects/{id}             详情
DELETE /projects/{id}             删除（级联删除其字幕版本）
```

### 字幕版本

```
POST   /projects/{id}/versions           上传字幕（format: auto/srt/vtt，自动识别）
GET    /projects/{id}/versions           版本列表
GET    /projects/{id}/versions/{vid}     版本详情（含解析后的 cue）
```

### 质检 / 修复 / 比较 / 导出

```
POST   /projects/{id}/versions/{vid}/qc        运行质检，返回问题清单（原因+修正候选）
POST   /projects/{id}/versions/{vid}/autofix   自动修复并保存为新版本，返回修复与冲突
GET    /projects/{id}/diff?from_version=&to_version=   比较两个版本（增/删/改）
GET    /projects/{id}/versions/{vid}/export?format=srt|vtt|report
                                               导出 SRT / WebVTT / JSON 质检报告
```

## 使用示例

```bash
# 1. 保存一套规则模板
curl -X POST localhost:8000/rule-templates -H 'Content-Type: application/json' -d '{
  "name": "网剧规范",
  "rules": {"max_cps": 20, "max_chars_per_line": 16, "max_lines": 2,
            "min_duration_ms": 800, "max_duration_ms": 7000,
            "min_gap_ms": 100, "max_offset_ms": 1200}
}'

# 2. 创建项目：25fps，两个镜头切点
curl -X POST localhost:8000/projects -H 'Content-Type: application/json' -d '{
  "name": "剧集样片", "frame_rate": 25.0,
  "shot_cuts": [6000, "00:00:12.500"], "rule_template_id": 1
}'

# 3. 上传 SRT 字幕
curl -X POST localhost:8000/projects/1/versions -H 'Content-Type: application/json' -d '{
  "label": "v1",
  "content": "1\n00:00:01,000 --> 00:00:01,300\n太短\n"
}'

# 4. 质检
curl -X POST localhost:8000/projects/1/versions/1/qc

# 5. 自动修复（生成新版本，原稿保留）
curl -X POST localhost:8000/projects/1/versions/1/autofix \
  -H 'Content-Type: application/json' -d '{"label": "v1-fixed"}'

# 6. 比较改动
curl "localhost:8000/projects/1/diff?from_version=1&to_version=2"

# 7. 导出
curl -OJ "localhost:8000/projects/1/versions/2/export?format=srt"
curl -OJ "localhost:8000/projects/1/versions/2/export?format=vtt"
curl "localhost:8000/projects/1/versions/2/export?format=report"
```

## 解析说明

- **SRT**：支持标准序号 + 时间轴 + 多行文本块；
- **WebVTT**：支持 `WEBVTT` 头、cue 标识符、cue 设置（`align`/`position` 等），
  跳过 `NOTE`/`STYLE`/`REGION` 块；
- 说话人标签（`<v 张三>`、`- 张三：`）与基础样式标签（`<i>`、`<b>`、`<u>`、
  `<font>`）在解析、修复、导出全流程保留；检测与分行按去标签后的可见字符计算；
- 上传时 `end <= start` 等非法时间轴会被拒绝（400）。

## 项目结构

```
app/
  main.py      FastAPI 应用与路由
  parsing.py   SRT/VTT 解析与序列化（保留说话人标签与样式）
  qc.py        质检规则（重叠/闪现/跨镜头/CPS/断行…）
  autofix.py   自动修复（断句、分行、时间窗调整、帧对齐、冲突处理）
  diffing.py   版本差异比较
  schemas.py   Pydantic 请求/响应模型
  models.py    SQLAlchemy ORM（规则模板/项目/版本）
  database.py  SQLite 引擎与会话
tests/
  test_api.py  端到端 API 测试
```
