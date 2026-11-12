"""Jenkins HTTP 请求层：动态发现任务并读取或触发构建。"""

import time
from urllib.parse import quote, urlsplit, urlunsplit

import requests

JENKINS_URL = "http://127.0.0.1:8080"
JOB_TREE = "jobs[name,fullName,url,_class]"


class JenkinsBuildAcceptedError(RuntimeError):
    """Jenkins 接收构建请求后，无法确认队列对应的构建编号。"""

    def __init__(self, queue_url, message):
        super().__init__(message)
        self.queue_url = queue_url


class JenkinsBuildOutcomeUnknownError(RuntimeError):
    """触发请求没有得到明确响应，无法判断 Jenkins 是否已接收。"""


class JenkinsBuildParametersRequiredError(ValueError):
    """参数化任务存在没有默认值且尚未提供的参数。"""

    def __init__(self, parameter_names):
        self.parameter_names = parameter_names
        super().__init__(
            "启动此 Jenkins 任务还需要参数：" + "、".join(parameter_names)
        )


def _api_json(url, username, token, params=None):
    response = requests.get(
        url,
        params=params,
        auth=(username, token),
        timeout=5,
        allow_redirects=False,
    )
    response.raise_for_status()
    if response.status_code != 200:
        raise requests.HTTPError("Jenkins 未返回 JSON 数据")
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Jenkins 返回的数据格式不正确")
    return data


def _job_url(full_name, server_url=None):
    if isinstance(server_url, str):
        target = urlsplit(server_url)
        base = urlsplit(JENKINS_URL)
        base_path = base.path.rstrip("/")
        if (
            target.scheme == base.scheme
            and target.netloc == base.netloc
            and target.path.startswith(f"{base_path}/job/")
        ):
            return urlunsplit(
                (target.scheme, target.netloc, target.path.rstrip("/") + "/", "", "")
            )

    segments = full_name.split("/")
    if not segments or any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError("Jenkins 任务全名格式不正确")
    return JENKINS_URL.rstrip("/") + "".join(
        f"/job/{quote(segment, safe='')}" for segment in segments
    ) + "/"


def _is_job_container(job_class):
    return (
        isinstance(job_class, str)
        and (job_class.endswith("Folder") or "MultiBranchProject" in job_class)
    )


def get_jenkins_jobs(username, token):
    """递归读取 Jenkins 中所有可构建任务，键为 Jenkins fullName。"""
    jobs = {}
    pending = [(JENKINS_URL.rstrip("/") + "/api/json", "")]
    visited = set()

    while pending:
        api_url, parent_name = pending.pop()
        if api_url in visited:
            continue
        visited.add(api_url)

        data = _api_json(api_url, username, token, params={"tree": JOB_TREE})
        children = data.get("jobs")
        if not isinstance(children, list):
            raise ValueError("Jenkins 任务列表格式不正确")

        for child in children:
            if not isinstance(child, dict) or not isinstance(child.get("name"), str):
                raise ValueError("Jenkins 任务信息格式不正确")
            full_name = child.get("fullName")
            if not isinstance(full_name, str) or not full_name:
                full_name = (
                    f"{parent_name}/{child['name']}" if parent_name else child["name"]
                )
            job_class = child.get("_class")
            job_url = _job_url(full_name, child.get("url"))

            if _is_job_container(job_class):
                pending.append((job_url + "api/json", full_name))
            else:
                jobs[full_name] = job_url

    return jobs


def query_latest_failed_build_logs(job_name, username, token, build_number=None):
    """读取最近失败构建或指定失败构建的编号与控制台日志；未失败时返回 None。"""
    job_url = get_jenkins_jobs(username, token).get(job_name)
    if job_url is None:
        raise ValueError("Jenkins 任务不存在或不可构建")

    if build_number is None:
        job = _api_json(
            job_url + "api/json",
            username,
            token,
            params={"tree": "builds[number,result]{0,100}"},
        )
        builds = job.get("builds")
        if not isinstance(builds, list):
            raise ValueError("Jenkins 构建历史格式不正确")

        failed_build = next(
            (
                build
                for build in builds
                if isinstance(build, dict) and build.get("result") == "FAILURE"
            ),
            None,
        )
        if failed_build is None:
            return None

        build_number = failed_build.get("number")
    else:
        build = _api_json(
            job_url + f"{build_number}/api/json",
            username,
            token,
        )
        if build.get("result") != "FAILURE":
            return None

    if not isinstance(build_number, int) or build_number < 1:
        raise ValueError("Jenkins 构建编号格式不正确")

    log_response = requests.get(
        job_url + f"{build_number}/consoleText",
        auth=(username, token),
        timeout=5,
        allow_redirects=False,
    )
    log_response.raise_for_status()
    if log_response.status_code != 200:
        raise requests.HTTPError("Jenkins 未返回构建日志")
    return build_number, log_response.text


def query_jenkins_build(job_name, username, token, build_number=None):
    """读取指定任务的最近一次构建，或指定编号的 Jenkins JSON 数据。"""
    job_url = get_jenkins_jobs(username, token).get(job_name)
    if job_url is None:
        raise ValueError("Jenkins 任务不存在或不可构建")
    build_path = "lastBuild" if build_number is None else str(build_number)
    return _api_json(
        job_url + f"{build_path}/api/json",
        username,
        token,
    )


def trigger_jenkins(job_name, username, token, parameters=None):
    """触发 Jenkins 任务并等待构建编号；区分请求失败与已接收但未能确认编号。"""
    job_url = get_jenkins_jobs(username, token).get(job_name)
    if job_url is None:
        raise ValueError("Jenkins 任务不存在或不可构建")

    job_data = _api_json(
        job_url + "api/json",
        username,
        token,
        params={
            "tree": (
                "property[parameterDefinitions[name,type,defaultParameterValue[value]]]"
            )
        },
    )
    definitions = [
        definition
        for prop in job_data.get("property", [])
        if isinstance(prop, dict)
        for definition in prop.get("parameterDefinitions", [])
        if isinstance(definition, dict) and isinstance(definition.get("name"), str)
    ]
    provided_parameters = {} if parameters is None else parameters
    if not isinstance(provided_parameters, dict) or not all(
        isinstance(name, str)
        and isinstance(value, (str, int, float, bool))
        for name, value in provided_parameters.items()
    ):
        raise ValueError("Jenkins 构建参数必须是字符串键值对象")

    if not definitions and provided_parameters:
        raise ValueError("此 Jenkins 任务不接受构建参数")

    parameter_names = {definition["name"] for definition in definitions}
    unknown_parameters = set(provided_parameters) - parameter_names
    if unknown_parameters:
        raise ValueError(
            "Jenkins 任务不存在这些参数：" + "、".join(sorted(unknown_parameters))
        )

    build_parameters = {}
    missing_parameters = []
    for definition in definitions:
        name = definition["name"]
        if name in provided_parameters:
            build_parameters[name] = str(provided_parameters[name])
            continue
        default = definition.get("defaultParameterValue")
        if isinstance(default, dict) and default.get("value") is not None:
            build_parameters[name] = str(default["value"])
        else:
            missing_parameters.append(name)

    if missing_parameters:
        raise JenkinsBuildParametersRequiredError(missing_parameters)

    build_url = job_url + ("buildWithParameters" if definitions else "build")
    try:
        response = requests.post(
            build_url,
            auth=(username, token),
            timeout=5,
            allow_redirects=False,
            data=build_parameters if definitions else None,
        )
    except requests.RequestException as error:
        raise JenkinsBuildOutcomeUnknownError(
            "触发请求没有收到 Jenkins 确认，构建是否已接收无法判断；为避免重复触发，请先检查 Jenkins。"
        ) from error
    try:
        response.raise_for_status()
    except requests.HTTPError as error:
        response_detail = " ".join(response.text.split())[:500]
        detail = f"HTTP {response.status_code}"
        if response_detail:
            detail += f": {response_detail}"
        raise requests.HTTPError(
            f"Jenkins 拒绝构建请求（{detail}）",
            response=response,
            request=response.request,
        ) from error
    queue_url = response.headers.get("Location")
    if not 200 <= response.status_code < 300:
        raise requests.HTTPError("Jenkins 未确认接收构建请求")
    if response.status_code != 201 or not queue_url:
        raise JenkinsBuildAcceptedError(
            queue_url,
            "Jenkins 返回成功响应，但没有提供有效的构建队列地址。",
        )

    queue_api_url = queue_url.rstrip("/") + "/api/json"
    max_attempts = 15

    for attempt in range(max_attempts):
        try:
            queue_data = _api_json(queue_api_url, username, token)
        except (requests.RequestException, TypeError, ValueError) as error:
            response = getattr(error, "response", None)
            detail = (
                f"HTTP {response.status_code}"
                if response is not None
                else type(error).__name__
            )
            raise JenkinsBuildAcceptedError(
                queue_url,
                f"Jenkins 已接收构建请求，但查询队列失败（{detail}）。",
            ) from error
        if queue_data.get("cancelled"):
            raise JenkinsBuildAcceptedError(queue_url, "Jenkins 已接收请求，但队列中的构建已取消。")

        executable = queue_data.get("executable")
        if executable:
            if not isinstance(executable, dict):
                raise JenkinsBuildAcceptedError(queue_url, "Jenkins 已接收构建请求，但构建信息格式不正确。")
            build_number = executable.get("number")
            build_url = executable.get("url")
            if not isinstance(build_number, int) or not isinstance(build_url, str):
                raise JenkinsBuildAcceptedError(queue_url, "Jenkins 已接收构建请求，但未返回有效构建编号。")
            return build_number, build_url

        if attempt < max_attempts - 1:
            time.sleep(2)
    raise JenkinsBuildAcceptedError(
        queue_url,
        "Jenkins 已接收构建请求，但等待分配构建编号超时。",
    )
