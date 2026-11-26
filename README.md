# 定制服装 3D 预览演示

这是个性定制（customdiy）的独立贴合引擎：把会员 DIY 各面效果图投射到第三方白胚 GLB 上。  
**不要**并入商城主 `docker-compose`，也不走 `php think async:work`。

## 启动

首次运行需要安装本地 Blender Python 运行库：

```bash
uv venv --python python3.11 .venv
uv pip install --python .venv/bin/python "bpy==4.5.12"
chmod +x start.sh
./start.sh
```

也可直接：

```bash
.venv/bin/python server.py
```

## 宝塔 Docker（推荐线上）

软件商店安装 Docker，把本目录传到服务器（不要传 `.venv`），然后执行：

```bash
cd /www/wwwroot/clothing-3d-demo
bash install.sh
```

它会检测 Docker、停旧容器、构建/升级镜像、启动，并测端口。通过后售卖设置填 `http://127.0.0.1:8765`，点「测一下」。

可选：`bash install.sh stop` / `restart` / `status` / `logs`

## 对接 customdiy（推荐：客户直接打开本网站）

商城不再自己连 Blender。会员点「3D预览」时，PHP 只生成一个本网站链接并把客户送过来：

```
http://3D服务器:8765/?model=白胚GLB&color=%23000000&front=正面图&back=背面图&title=球服
```

- `model`：白胚 GLB 公网地址（必填，缺了就走普通演示页）
- `color`：衣身颜色 `#RRGGBB`
- `front` / `back` / `left_sleeve` / `right_sleeve` / `pants`：各面效果图公网地址，有哪面传哪面
- `title`：显示在左上角的名称
- `token`：容器设置了 `CUSTOMDIY_BLENDER_TOKEN` 时才需要

预览模式会隐藏颜色/面料/上传面板，自动把大体积白胚压缩为轻量缓存版（`model-lite-v1.glb`，1536px JPEG + Draco 网格压缩），并由 Blender 仅导出几百 KB 的贴片网格（`decals.glb`）与白胚并行加载叠加；同一底模 + 颜色 + 效果图组合的结果会缓存到 `output/cache/results/`，第二次打开秒出。没有效果图时走 `GET /api/model?url=` 重定向到 Nginx 静态缓存的轻量白胚。

售卖设置里的「3D网站地址」必须是客户手机能访问的公网地址，不能填 `127.0.0.1`。

## 老方式：PHP 调接口

`POST /api/generate` 现支持：

```json
{
  "base_model_url": "https://.../garment.glb",
  "color": "#ffffff",
  "patterns": [
    { "placement": "front", "image_url": "https://.../front.png", "scale": 1, "position_x": 0.4, "position_y": 0.2 }
  ]
}
```

- `base_model_url`：第三方白胚。不传时回退 `assets/custom-garment.glb`
- `image_url`：各面 PNG/JPG/WebP，也可继续用旧的 `image_data` base64
- 位点 `front` / `back` / `left_sleeve` / `right_sleeve` / `pants`

查询：`GET /api/tasks/{task_id}`。完成态为 `completed`，`model_url` 可能是相对路径 `/output/jobs/.../model.glb`。

## 演示页

访问 `http://127.0.0.1:8765` 仍可手动导入 GLB、上传图案并生成样衣。

真实模型接入建议：

- 使用 `GLB` 格式并内嵌贴图
- 模型单位和朝向正确，脚底位于模型最低点
- 三角面控制在 3 万至 8 万左右
- 贴图优先使用 2K，文件尽量控制在 15MB 以内
- 需要动态换色的服装网格名称包含 `shirt`、`cloth`、`garment`、`dress`、`top`、`jacket`、`polo` 或对应中文关键词

## 文件说明

- `index.html`：页面结构
- `styles.css`：桌面和手机响应式样式
- `app.js`：Three.js 场景、模拟模型、交互与 GLB 导入
- `server.py`：静态页面、异步任务、外部底模/图案下载和生成结果
- `blender_apply_pattern.py`：Blender 曲面投射与 GLB 导出脚本
- `install.sh` / `安装.sh`：宝塔一键安装 / 升级 / 启停
- `INSTALL.txt`：解压后怎么装
- `start.sh`：本机启动（不用 Docker）
- `docker-compose.yml`：仅本服务，由 `install.sh` 调用
- `vendor/`：本地 Three.js 依赖
