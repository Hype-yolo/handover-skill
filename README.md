# Handover · 接手本机其他 Claude 会话

换账号、额度用完、会话中断之后，让新的 Claude 接着把活干完。

**读出旧会话做到哪 → 找出哪条指令还没做完、哪个问题还在等你回答 → 对照磁盘上的真实文件 → 接着做。**

## 为什么需要它

Claude Code 不管用哪个账号登录，会话记录都写在本机的 `~/.claude/projects` 里。桌面端的会话列表只显示当前账号的会话，但记录文件一直在本机。换了账号以后，新会话其实读得到旧会话。

这个 Skill 把这些记录读成一份交接单：

- 每个会话的标题、最后活动时间，以及状态：额度用完中断（含恢复时间）、最后一条消息没回复、最后在等你回答、正常结束、可能还在跑。
- 时间相近或额度恢复时间相同的会话归成一批，标出「上一批中断」里要接手的会话。
- 最后一条用户指令是没执行，还是做到一半被打断。
- 对方停在问你什么问题、给了哪些选项。
- 改过哪些文件、什么时间改的。
- 用户原话和附图路径，与工具输出、后台通知、其他会话发来的消息分开标注。

## 安装

把下面这句话发给 Claude Code：

```text
帮我安装这个 Skill：https://github.com/Hype-yolo/handover-skill
```

也可以手动克隆到 Claude Code 的 skills 目录：

```bash
git clone https://github.com/Hype-yolo/handover-skill ~/.claude/skills/handover
```

国内网络可以用 Gitee 镜像：

```bash
git clone https://gitee.com/Hypeyolo/handover-skill ~/.claude/skills/handover
```

需要 Python 3.9 或更高版本（macOS 自带的即可），不用装第三方依赖。

## 使用

在新会话里输入 `/handover`，或者直接说：

```text
额度用完了，接手刚才那几个会话
另一个账号做到哪了？
```

Claude 会先列出最近的会话，跟你确认要接哪几个，再逐个读记录、核对磁盘上的文件，说明每个会话做到哪、准备怎么接，然后继续做。

脚本也可以单独运行：

```bash
H=~/.claude/skills/handover/scripts/sessions.py
python3 -I $H list                # 当前目录项目最近 24 小时的会话；--hours 72 / --dir <路径> / --all
python3 -I $H show <id 前缀或标题>  # 某个会话的交接详情；--turns 0 看全部轮次，--full 不截断
python3 -I $H grep <文字>          # 查某句话出现在哪个会话
python3 -I $H codex --hours 48     # 顺带列出最近的 Codex 会话（只读）
```

## 边界

- **只读**：不修改、不删除任何 `.jsonl` 记录，也不联网，输出只显示在你自己的终端里。
- **只把你亲手发的消息当指令**：上级会话写的任务说明、工具输出、网页内容、其他会话发来的消息只当资料，不当指令执行。
- **以磁盘为准**：记录里写的「已完成」只是那个会话自己的说法，接手前要先看实际文件。
- **看不到的部分**：云端会话、别的电脑上的会话不在本机记录里，读不到。
- Claude Code 的记录格式没有公开文档，以后版本更新可能导致部分字段读不出来。

## 验证范围

2026-10-21 在 macOS 上用 Claude Code 桌面端的真实会话记录验证了 `list`、`show`、`grep`、`codex` 四个命令；Python 3.9（macOS 自带）、3.14、3.15 实际运行通过。Linux 和 Windows 尚未实际测试。

## 许可

作者：Hype。采用 [MIT License](LICENSE)。
