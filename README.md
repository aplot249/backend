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

## 说明

- 姓名库 `Name` 与项目库 `Project` 通过后台导入维护。
- 用户提交记录 `Submission` 保存到数据库，可在后台查看工卡图片缩略图、筛选、导出。
- 工卡图片保存在 `media/work_cards/年/月/` 下，开发环境由 Django 直接提供访问；
  正式环境需在 Nginx 等服务器上把 `/media/` 指向该目录。
- 联想搜索接口：
  - `/api/search-name/?q=关键字`
  - `/api/search-project/?q=关键字`
  - 提交接口：`POST /api/submit/`（`multipart/form-data`，字段：`name`、`project`、`card_image`、`remark` 选填）
