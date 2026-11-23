# 大肥鱼 · DevOps 自助发布助手

一个通过飞书对话操作本地 Jenkins、查询 Kubernetes 测试环境和检索飞书文档的 Python 机器人。当前处于本地 MVP 联调阶段：Jenkins 主链路已有联调记录，Holmes 和知识检索仍待端到端验收。各模块的验证记录见 [项目进度](项目进度.md)。

## 工作方式

飞书长连接收到文本消息后，机器人在后台先给原消息加「敲键盘」表情，再调用 DeepSeek。模型选择工具并填写参数，Python 用 Pydantic 校验后执行；工具结果返回 DeepSeek，最终答复以飞书富文本回复原消息，保留 Markdown 的标题、列表、代码块和文档链接。处理结束后移除表情，表情接口失败时仍继续处理消息。

大肥鱼会把最近 4 轮成功回复的问答带给模型。私聊按会话记忆，群聊按群、发言人和话题隔离。同一会话按收到的顺序处理。记忆保存在进程内，闲置 30 分钟后不再使用，重启清空；不会补读飞书历史消息。

闲聊和公开话题可直接回答。飞书回复底部显示当前消息对应的 DeepSeek 请求次数与输入、输出、合计 Token，按 API 返回的用量累加。未返回用量的请求会标明，统计行不进入会话记忆。

处理终端或日志截图时，机器人使用飞书消息资源接口读取当前消息的图片，再用 macOS Vision 本地提取文字。单条最多识别 3 张，每张最多 8 MiB，需要 macOS 和 Xcode 命令行工具。模型接收 OCR 摘录，原图在本地处理。下载或识别失败时回复实际原因。

图片下载若报 `99991672`，在飞书开放平台检查应用身份的「获取单聊、群组消息」权限（`im:message:readonly`），以接口实际返回的 scope 为准。确认权限随应用版本生效后重发截图。

| 能力 | 当前行为 |
| --- | --- |
| Jenkins 构建 | 列出任务、查构建状态、读失败日志。明确要求触发时，按同一构建编号跟踪结果；失败时读取对应构建的日志 |
| Kubernetes 诊断 | 支持未知目标时的节点、Pod 和告警事件概览，以及工作负载、Pod、路由、PVC、网络策略和单容器日志的定向查询 |
| Prometheus 指标 | 模型按问题编写 PromQL，结合查询错误或空结果调整表达式；可与 Holmes 现场和 Jenkins 构建记录一起排查 |
| 飞书知识检索 | 以飞书应用身份读取文件夹及子文件夹中的飞书 Docx，在 SQLite FTS5 中按正文片段检索。具体问题可命中同一篇文档的多段；全文总结按标题读取原文，长文可续页 |

## 本地启动

项目当前使用 Python 3.13 验证。知识检索需要 Python 的 SQLite 支持 FTS5 和 `trigram` 分词。请从项目根目录运行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
```

按需要填写项目根目录的 `.env`。如果进程环境已经设置同名变量，以进程环境为准。

| 配置项 | 用途 |
| --- | --- |
| `APP_ID`、`APP_SECRET` | 飞书自建应用凭据，用于机器人收发消息和读取知识库 |
| `DEEPSEEK_API_KEY` | DeepSeek API Key；主对话和 Holmes 共用 |
| `BRAVE_SEARCH_API_KEY` | 可选。公开网页搜索使用的 Brave Search API Key |
| `JENKINS_USERNAME`、`JENKINS_API_TOKEN` | Jenkins 用户和 API Token；查询需要读取权限，触发测试任务还需要该任务的构建权限 |
| `PROMETHEUS_URL`、`PROMETHEUS_ENVIRONMENT` | 可选；同一个测试环境的 Prometheus 地址和环境名。地址只填服务入口，不含 `/api/v1/query_range` |
| `PROMETHEUS_BEARER_TOKEN` | 可选；服务入口需要 Bearer 认证时使用只读凭据 |
| `FEISHU_KNOWLEDGE_FOLDER_TOKEN` | 知识库文件夹 token，只填 token；飞书应用需要能列出文件夹并读取其中的 Docx |

飞书开放平台的 API 权限和目标文件夹的访问权限要分别配置。已验证可在目标文件夹点「分享 → 添加协作者」，直接搜索并添加与 `APP_ID` 对应的机器人「大肥鱼」，授予「可阅读」。如果列出文件夹时报 `1061004`，先检查这条协作者授权是否存在。上传的 `.md` 附件会跳过；验收请在文件夹内新建飞书原生文档。

在飞书开放平台启用应用的机器人能力，配置长连接事件订阅并添加 `im.message.receive_v1`，确保应用有收取测试消息和回复消息的权限。配置生效后，用可访问该机器人的飞书账号发送文本消息。

「敲键盘」使用飞书的消息表情回复接口。在应用身份下开通 `im:message.reactions:write_only`（发送、删除消息表情回复），或使用已生效且覆盖该接口的消息权限。

Jenkins 地址当前在 [trigger_jenkins.py](trigger_jenkins.py) 中设为 `http://127.0.0.1:8080`；使用本地 Jenkins 时，先确认机器人运行环境能访问该地址。

```bash
python feishu_python.py
```

联调时只运行一个机器人实例。如果飞书出现重复回复或仍收到旧版固定回复，先检查是否有另一个进程使用同一应用凭据。

## 验收示例

1. **Jenkins：**在飞书发送「列出当前 Jenkins 任务」，再用实际任务名发送「查询 Jenkins 任务『任务名』最近一次构建状态」。需要验证失败链路时，可在本地 Jenkins 新建 Pipeline，使用 [Jenkinsfile.failure-demo](Jenkinsfile.failure-demo) 作为脚本，再明确要求机器人触发该测试任务。默认 `FAIL_STAGE=Checkout` 会模拟 Checkout 失败；检查回复里的构建编号和日志证据是否对应。参数化任务使用 Jenkins 的默认参数，缺少必填参数时需先补齐。
2. **本地集群诊断：**在 `.env` 设置 `HOLMES_KUBECONFIG`（独立的本地配置文件路径）和 `HOLMES_KUBE_CONTEXT`（准确的 context 名称）。重启后，先问已知命名空间里的服务状态，再发带链接的 TLS 故障追问。确认机器人沿用同一目标，并用 Ingress、Service 和 EndpointSlice 解释目前能定位到哪一步。Ingress 的 TLS 引用仅供核对配置，握手结果仍需从客户端取证。
3. **飞书知识库：**在已授权的测试文件夹放一篇非敏感的飞书 Docx，执行 `python feishu_knowledge.py` 强制同步。确认输出的 `status` 为 `synced`、`documents_indexed` 大于 0。用文档后半段的关键词验证片段检索，再问「列出《文档标题》里的所有项目」，核对答复是否覆盖末尾条目并附原文链接。全文读取每页最多 18,000 字符，若有下一页会返回续读位置。常规查询会复用 15 分钟内的索引；修改文档后可强制同步。索引文件含文档正文，和 `.env` 一样只保留在本地。
4. **联合排查：**配置一个只覆盖目标测试环境的 Prometheus，再用真实标签和任务名询问「联合排查测试环境 `<命名空间>` 的 `<服务>` 最近 30 分钟的 5xx 异常；Prometheus job 是 `<job>`，关联 Jenkins 任务是 `<任务全名>`」。对照 Prometheus 峰值时间、Holmes 检查目标和 Jenkins 构建编号。构建完成不能证明已部署；缺少某一路证据时，答复应标出缺口。

Prometheus 地址从机器人运行环境出发确认：已有 Grafana 时，可请管理员查看对应 Prometheus 数据源的服务地址；Grafana 页面本身不是 Prometheus API。如果本地运行机器人、Prometheus 只在测试集群内，可先用 `kubectl get svc -A` 找服务名、命名空间和服务端口，再用 `kubectl -n <命名空间> port-forward svc/<服务名> 19090:<服务端口>` 临时转发。转发保持运行时，将 `PROMETHEUS_URL` 设为 `http://127.0.0.1:19090`，再用 `curl -fsS -G http://127.0.0.1:19090/api/v1/query --data-urlencode 'query=up'` 验证 API。环境名由你按实际环境填写，例如 `PROMETHEUS_ENVIRONMENT=uat`；机器人用它匹配用户要查的环境，它不会过滤 Prometheus 数据。

模型可以自行写 PromQL，Python 只向配置的地址调用 `query_range`：窗口 5–120 分钟、每 30 秒取点、每轮最多执行 4 次 Prometheus 查询，每次最多返回 8 条序列的摘要。PromQL 可能读取该账号可见的任意指标，测试环境应使用隔离的数据源和只读凭据。没有样本不自动按零处理。

项目当前用于本地联调和测试环境验收，生产发布与自动回滚暂不在范围内。

不清楚目标时可问「帮我扫一下本地集群，有异常就继续定位」。概览会优先返回有异常信号的资源，模型可据此再查明确的 Pod、工作负载或依赖。日志查询需要明确命名空间、Pod 和容器，每次最多返回最近 30 分钟的 40 行、8 KiB。诊断结果会发送给 DeepSeek，请使用不含敏感日志的本地测试集群。

## 代码入口

| 文件 | 职责 |
| --- | --- |
| [feishu_python.py](feishu_python.py) | 飞书长连接、后台消息处理、回复原消息 |
| [deepseek.py](deepseek.py)、[agent.md](agent.md) | 模型请求、工具调用循环、机器人行为规则 |
| [tool.py](tool.py)、[models.py](models.py) | 工具注册、参数校验与分发 |
| [jenkins_tools.py](jenkins_tools.py)、[trigger_jenkins.py](trigger_jenkins.py) | Jenkins 工具封装和 API 调用 |
| [holmes_ask.py](holmes_ask.py)、[feishu_knowledge.py](feishu_knowledge.py) | Holmes 诊断、飞书文档同步与检索 |

本地 GitLab 配置检查脚本和验收产物不属于机器人启动链路，不随本仓库提交。
