# 表单统计后端（Django + SimpleUI）

一个用于「工作内容统计」的后端项目，包含：

- 表单页面：`name`（必填）、`project`（必填）、`card_image`（工卡图片，必填）、`remark`（工作内容，选填）
- `name` / `project` 支持联想搜索（从后台导入的 Excel 数据中匹配）
- 工卡图片支持 jpg / png / webp / bmp / gif，单张不超过 10MB，提交前可预览
- 后台（SimpleUI）支持 Excel 导入姓名库、项目库，并导出提交记录（含工卡图片链接）

## 目录结构

```
backend/
├── manage.py
├── requirements.txt
├── config/            # 项目配置
├── formapp/           # 应用（模型、视图、后台）
└── templates/form.html  # 前端表单页面
```

## 快速开始

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 初始化数据库

```bash
python manage.py migrate
```

### 3. 创建后台管理员账号

```bash
python manage.py createsuperuser
```

### 4. 启动服务

```bash
python manage.py runserver
```

- 表单页面：http://127.0.0.1:8000/
- 数据整理页：http://127.0.0.1:8000/summary/（需先登录后台）
- 后台管理：http://127.0.0.1:8000/admin/

## Excel 导入（姓名库 / 项目库）

1. 登录后台 `/admin/`
2. 进入「姓名库」或「项目库」
3. 点击右上角「导入」(IMPORT) 按钮，选择 Excel 文件（.xlsx）

> Excel 文件要求：第一行为表头，表头必须包含 `name` 列（或 `姓名` 列），
> 内容即为要导入的姓名 / 项目名称。导入时会以 `name` 字段去重。

姓名库还包含以下字段（可在 Excel 里一并提供，也可在后台「姓名库」列表直接编辑）：

| 字段 | 说明 |
|---|---|
| `employee_no` | 工号 |
| `passport_no` | 护照号 |
| `hourly_rate` | 工时系数，即每小时金额（元/小时），用于与工作小时数相乘计算总金额 |

示例 Excel（姓名库）：

| name | employee_no | passport_no | hourly_rate |
|------|-------------|-------------|-------------|
| 张三 | E001        | P1234567    | 25.5        |
| 李四 | E002        |             | 30          |

> 只导入 `name` 列也可以，已有记录的工号 / 护照号 / 工时系数不会被清空。

## Excel 导出（提交记录）

1. 登录后台 `/admin/`
2. 进入「提交记录」
3. 点击右上角「导出」(EXPORT) 按钮，选择格式（xlsx）即可导出所有提交记录

## 从考勤表补全身份信息（import_attendance.py）

把 `Latest list of attendance.xlsx`（后端根目录）里的工种、护照号、单价写入「姓名库」的
工号、护照号、工时系数：

```bash
cd backend
python import_attendance.py            # 写入数据库
python import_attendance.py --dry-run  # 只预览，不写库
python import_attendance.py --file 路径.xlsx
```

- 按第 2 行表头自动定位列（姓名 / 工种 / 护照号 / 单价，找不到则用 B、D、E、F 兜底），第 3 行起读数据
- 姓名匹配忽略大小写与多余空格；仍匹配不上时再按「只保留字母数字」宽松匹配一次
- 一人只取 Excel 中的第一条，重复行会单独报告；只想更新有值的字段，值相同则跳过
- 运行结束会打印：更新人数、无变化人数、空行、未匹配姓名、重复行

## 数据整理页（/summary/）

以「姓名库」为基准核对每个人的提交情况，需先登录后台再访问：

- 顶部统计：姓名库总人数 / 已提交 / 未提交 / 多次提交 / 跨多项目 / 累计工时（全员）
- 姓名后面直接显示该工人的「工号」「护照号」（取自姓名库，未填写则不显示）
- 每个工人的**总工时**（`WorkHour` 里该姓名所有项目的小时数累加，与项目数量无关）：
  头部右侧「总工时 X 小时」标签，摘要行里也带「累计工时 X 小时」
- 未提交的人：红色标记 + 文案「该人尚未提交记录：姓名库中存在此人，但提交记录里没有该姓名的数据。」
- 已提交的人：先按「项目」左右分列，同一个人在不同项目下的提交各自独立成列；
  每个项目列显示项目名与该列提交次数，项目列之间按该项目最近一次提交时间倒序排列
- 项目列内部按时间倒序列出该项目的每一次提交：
  全局最新的一条带「最新提交」徽标并高亮；其余列的首条为「该项目最新」；再往后为「第 N 次提交」
- 该人涉及多个项目时，头部额外显示「跨 N 个项目」标签
- 每条提交显示：提交时间、工作内容（未填写显示「未填写」）、工卡图片缩略图（项目名已由所属列标题给出）
- 点击缩略图弹出「图片编辑器」（本页弹窗，不新开窗口），标题显示「姓名 · 工号 · 护照号」，功能：
  - 旋转：左转 / 右转 90°，以及 -180°~180° 的精细旋转滑块
  - 裁剪：四周 8 个控制点 + 边中点可拖拽，也可整体拖动选区；「全选」「复位」快捷按钮
  - 非选区区域变暗，实时显示原图尺寸与裁剪后像素尺寸
  - 点「保存并替换原图」：在浏览器端用 Canvas 处理（导出 JPEG），提交到后端覆盖原图，
    页面缩略图即时刷新；旧图片文件会自动删除
  - 编辑接口：`POST /api/submission/<id>/update-card/`（需后台登录 + CSRF）
- 弹窗右侧「工天数据」面板（按工卡人工录入工时）：
  - 周期为「上月 21 日 ~ 本月 20 日」（如 10 月周期 = 09-21 ~ 10-20），默认取该提交记录所在的周期
  - 按真实日历生成（周日起始，跨月则显示两个月的日历），周期外的日期置灰不可填
  - 每天一个数字输入框（0~24，步长 0.5），没有工作就填/保持 0；实时显示合计小时数与出勤天数
  - 「保存工天数据」写入后端，同一提交记录同一天重复保存为覆盖更新
  - 数据模型 `WorkHour`：所属提交记录（工卡）、工人姓名、项目、工作日期、工作小时数；
    唯一约束为「**人 + 项目 + 日期**」，一人一项目一天只有一条记录，重复保存为覆盖更新
  - **只有「该人 + 该项目」最新提交的工卡能录入 / 保存工天数据**，历史工卡只读：
    列表里历史工卡带「历史工卡·只读」标记，弹窗内有橙色提示条，日期输入框与「保存工天数据」按钮均禁用，
    后端 `save` 接口也会拦截（返回 400）
  - 数据统一挂在最新工卡上：历史工卡上的工时会归并到最新工卡（迁移 `0006` 已处理存量数据）
  - 接口：`GET /api/submission/<id>/work-hours/`、`POST /api/submission/<id>/work-hours/save/`
  - 后台「工时记录」支持按日期/项目筛选、搜索、Excel 导出

## 导出 Excel（/summary/export/）

页面顶部选择考勤周期后点「导出 Excel」，后端按样表模板生成统计表：

- 样表模板：`样表模板.xlsx`，放在**后端根目录**（或上一级项目根目录）均可，程序会自动查找
- 模板结构：第 1 行标题（合并单元格，导出时改为 `MONTH OF ATTENDANCE (月份 年)`）、第 2 行表头、第 3 行起写数据
- 列对应关系：

| 样表列 | 内容 |
|---|---|
| 序号 | 自动填充 1、2、3… |
| 姓名/Name | 工人姓名（`WorkHour.name`） |
| SITE LOCATION | 项目名（`WorkHour.project`），一人多个项目时用 ` / ` 连接 |
| 工种/ Employee.No | 该工人的工号 |
| 备注（护照号码 | 该工人的护照号 |
| 单价/H | 该工人的工时系数（姓名库 `hourly_rate`） |
| 1 ~ 31 | 周期内的第 1~31 天：第 1 列 = 起始月 21 号，第 2 列 = 22 号 … 依次顺延到次月 20 号；同一天若有多个项目，小时数相加后填一格 |
| Total Hour | 该工人在周期内所有天的工时之和 |
| 金额/RM | Total Hour × 单价（保留 2 位小数） |

- 周期不足 31 天（起始月为 30 天）时，末尾列留空
- **只抽取「最新提交工卡」关联的工时数据**，历史工卡上的数据不会进入导出
- 周期下拉默认选中「最近一个有工时记录的周期」，参数 `?month=YYYY-MM`（`YYYY-MM` 指周期结束月，即「本月 20 日」所在月）
- 需后台登录访问，接口：`GET /summary/export/?month=2026-10`

## 说明

- 姓名库 `Name` 与项目库 `Project` 通过后台导入维护。
- 用户提交记录 `Submission` 保存到数据库，可在后台查看工卡图片缩略图、筛选、导出。
- 工卡图片保存在 `media/work_cards/年/月/` 下，开发环境由 Django 直接提供访问；
  正式环境需在 Nginx 等服务器上把 `/media/` 指向该目录。
- 联想搜索接口：
  - `/api/search-name/?q=关键字`
  - `/api/search-project/?q=关键字`
  - 提交接口：`POST /api/submit/`（`multipart/form-data`，字段：`name`、`project`、`card_image`、`remark` 选填）
  - 工卡图片更新接口：`POST /api/submission/<id>/update-card/`（字段：`card_image`，需后台登录）

## 生产环境部署（`DEBUG=False`）

静态文件与上传文件在 `DEBUG=False` 时不会由 `runserver` 自动提供，需要额外处理：

### 1. 收集静态文件

```bash
python manage.py collectstatic     # 输出到 staticfiles/
```

### 2. 用 Nginx 托管 static / media（推荐）

```nginx
location /static/ {
    alias /srv/form-backend/staticfiles/;
    expires 30d;
    access_log off;
}

location /media/ {
    alias /srv/form-backend/media/;
    expires 30d;
    access_log off;
}
```

> `alias` 路径结尾的 `/` 要与 `location` 一致；同时确保运行 Django 的账号对 `media/` 有写权限（上传用），Nginx 账号有读权限。

### 3. 未使用 Nginx 时的兜底方案

项目已内置：当 `DEBUG=False` 时，`/media/` 由 Django 的 `protected-media` 路由提供，
**仅允许已登录的后台用户访问**（工卡属于敏感信息）。此时无需额外配置即可在后台看到图片，
但性能不如 Nginx，正式环境仍建议按第 2 步配置。

### 4. 时区问题

若后台按时间筛选/分组时报错 `Database returned an invalid datetime value`，
说明 MySQL 未加载时区表，在服务器上执行：

```bash
mysql_tzinfo_to_sql /usr/share/zoneinfo | mysql -u root -p mysql
```
