# OmniPet

[English](README.en.md)

> **Alpha（`0.1.0a1`）**：接口和项目文件仍可能变化；请在正式使用前检查生成结果。

OmniPet 是生成、QA、修复、打包和发布桌宠 sprite v2 的开源引擎。它通过官方 OpenAI SDK 调用 `gpt-image-2`，并把可恢复的本地工作状态、checkpoint 与最终可安装包分开管理。

只想安装桌宠？请访问公开目录 [OmniPets](https://github.com/0mn1si2i5/OmniPets)。创作者可在本地或私有生产仓中使用 OmniPet，完成后导出已校验的公开发布包。

## 安装与开始

需要 Python 3.12 或更高版本：

```sh
python -m venv .venv
.venv/bin/python -m pip install omnipet
export OPENAI_API_KEY="your-key"
omnipet pet init my-pet
omnipet pet validate my-pet
omnipet hatch my-pet
omnipet status my-pet
```

`OPENAI_API_KEY` 由 OpenAI SDK 从进程环境读取；不要提交它。图像请求可能产生费用，并会将提示词和已配置的参考图发送给 OpenAI——请只使用有权处理的内容。

首次 `hatch` 会生成 base candidate 并暂停；按 `status` 给出的下一步继续审批、QA 与打包即可。常用命令：

```sh
omnipet approve my-pet --stage base --note "identity accepted"
omnipet hatch my-pet
omnipet package my-pet --check
omnipet package my-pet
omnipet release export my-pet --output release-work/my-pet
omnipet release verify release-work/my-pet
```

完整 QA 与发布流程见 [生成工作流](docs/generation-workflow.md) 和 [package review](docs/package-review.md)。

## 工作目录

```text
pets/my-pet/                 应提交的项目输入、checkpoint 与最终 dist/
.omnipet/runs/my-pet/        忽略的可恢复运行状态
.omnipet/archives/           忽略的运行状态替换归档
```

`.omnipet` 不是某个仓库专属目录：命令在哪个项目仓运行，就可能在那里生成它。官方生产通常在 `OmniPet-Production` 保存持久化项目文件；无论在生产仓、引擎开发仓还是个人项目中，`.omnipet` 都应保持本地忽略、不提交。

## SuShi 示例

[SuShi v1.0.1](https://github.com/0mn1si2i5/OmniPets/tree/main/pets/sushi) 是完整的公开发布示例，包含已校验的 `pet.json`、sprite 图集与预览。公开仓只放可安装产物；生产 checkpoint 和修复记录不随发布包公开。

开发贡献请见 [CONTRIBUTING.md](CONTRIBUTING.md)。
