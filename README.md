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

示例 Excel：

| name     |
|----------|
| 张三     |
| 李四     |

## Excel 导出（提交记录）

1. 登录后台 `/admin/`
2. 进入「提交记录」
3. 点击右上角「导出」(EXPORT) 按钮，选择格式（xlsx）即可导出所有提交记录

## 数据整理页（/summary/）

以「姓名库」为基准核对每个人的提交情况，需先登录后台再访问：

- 顶部统计：姓名库总人数 / 已提交 / 未提交 / 多次提交
- 未提交的人：红色标记 + 文案「该人尚未提交记录：姓名库中存在此人，但提交记录里没有该姓名的数据。」
- 已提交的人：按时间倒序列出每一次提交，第一条带「最新提交」徽标并高亮，其余为「第 N 次提交」
- 每条提交显示：提交时间、项目、工作内容（未填写显示「未填写」）、工卡图片缩略图
- 点击缩略图弹出「图片编辑器」（本页弹窗，不新开窗口），功能：
  - 旋转：左转 / 右转 90°，以及 -180°~180° 的精细旋转滑块
  - 裁剪：四周 8 个控制点 + 边中点可拖拽，也可整体拖动选区；「全选」「复位」快捷按钮
  - 非选区区域变暗，实时显示原图尺寸与裁剪后像素尺寸
  - 点「保存并替换原图」：在浏览器端用 Canvas 处理（导出 JPEG），提交到后端覆盖原图，
    页面缩略图即时刷新；旧图片文件会自动删除
  - 编辑接口：`POST /api/submission/<id>/update-card/`（需后台登录 + CSRF）

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
