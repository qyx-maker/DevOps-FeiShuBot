"""为集群诊断收集 Docker Desktop 本地 Kubernetes 的只读证据。"""

import ipaddress
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from app_config import load_project_environment

TOOL_NAME = "ask_holmes"
MAX_ROWS = 20
MAX_LOG_BYTES = 8192
WORKLOADS = ("deployments.apps", "statefulsets.apps", "daemonsets.apps", "jobs.batch")

# Kubernetes 只输出诊断需要的字段，不把注解或 Secret 内容交给模型。
SERVICE_COLUMNS = (
    "NAME:.metadata.name,TYPE:.spec.type,CLUSTER_IP:.spec.clusterIP,"
    "SELECTOR:.spec.selector,PORTS:.spec.ports[*].port,TARGET_PORTS:.spec.ports[*].targetPort"
)
SLICE_COLUMNS = (
    "NAME:.metadata.name,SERVICE:.metadata.labels.kubernetes\\.io/service-name,"
    "ADDRESSES:.endpoints[*].addresses[*],READY:.endpoints[*].conditions.ready,"
    "PORT_NAMES:.ports[*].name,PORTS:.ports[*].port"
)


def _host_matches(rule: str, host: str) -> bool:
    rule = rule.lower().rstrip(".")
    return not rule or rule == host or (
        rule.startswith("*.") and host.endswith(rule[1:]) and host.count(".") == rule.count(".")
    )


def _ingress_evidence(raw: str, host: str | None, service: str | None) -> dict:
    routes = []
    for item in json.loads(raw)["items"]:
        spec = item.get("spec", {})
        rules = [
            {"host": rule.get("host", ""), "paths": [
                {
                    "path": path.get("path"),
                    "path_type": path.get("pathType"),
                    "backend": path.get("backend", {}),
                }
                for path in rule.get("http", {}).get("paths", [])
            ]}
            for rule in spec.get("rules", [])
        ]
        tls = spec.get("tls", [])
        default_backend = spec.get("defaultBackend")
        if host:
            rules = [rule for rule in rules if _host_matches(rule["host"], host)]
            tls = [entry for entry in tls if not entry.get("hosts") or any(
                _host_matches(name, host) for name in entry["hosts"]
            )]
            if not rules and not tls and not default_backend:
                continue
        elif service and not (
            any(path["backend"].get("service", {}).get("name") == service
                for rule in rules for path in rule["paths"])
            or (default_backend or {}).get("service", {}).get("name") == service
        ):
            continue
        routes.append({
            "name": item["metadata"]["name"],
            "ingress_class": spec.get("ingressClassName"),
            "addresses": item.get("status", {}).get("loadBalancer", {}).get("ingress", []),
            "rules": rules,
            "default_backend": default_backend,
            "tls_references": tls,
        })
    return {"items": routes[:MAX_ROWS], "omitted_items": max(0, len(routes) - MAX_ROWS)}


def _collect(query: list[str], resource: str, options: list[str], host: str | None, service: str | None) -> dict:
    raw = _kubectl(query + ["get", resource, *options])
    if resource == "ingresses.networking.k8s.io":
        return _ingress_evidence(raw, host, service)
    rows = raw.splitlines()
    limit = MAX_ROWS + 1  # 第一行是 kubectl 的表头。
    selected = rows[:limit]
    if len(rows) > limit and resource == "events":
        selected = rows[:1] + rows[-MAX_ROWS:]
    return {
        "lines": [line[:600] for line in selected],
        "omitted_lines": len(rows) - len(selected),
        "clipped_lines": sum(len(line) > 600 for line in selected),
    }


def _kubectl(command: list[str]) -> str:
    try:
        return subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=8,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("kubectl 查询失败，请检查本地集群是否运行及命名空间是否可读。") from error


def run_holmes_tool(arguments: dict[str, str]) -> str:
    """限定 kubeconfig、上下文和命名空间，再将固定查询结果交给 DeepSeek。"""
    load_project_environment()
    question = arguments["question"]
    namespace = arguments["namespace"]
    result = {"question": question, "namespace": namespace, "source": "local_kubectl"}
    try:
        config = os.getenv("HOLMES_KUBECONFIG", "").strip()
        context = os.getenv("HOLMES_KUBE_CONTEXT", "").strip()
        if not config or not context or not Path(config).expanduser().is_file():
            raise RuntimeError("请配置有效的 HOLMES_KUBECONFIG 和 HOLMES_KUBE_CONTEXT。")

        kubectl = ["kubectl", "--kubeconfig", str(Path(config).expanduser()), "--context", context]
        server = _kubectl(kubectl + [
            "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}",
        ])
        hostname = urlsplit(server).hostname
        try:
            local = hostname is not None and ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            local = hostname in {"localhost", "kubernetes.docker.internal"}
        if not local:
            raise RuntimeError("本地测试仅接受回环地址或 Docker Desktop 的 Kubernetes 地址。")

        query = kubectl + ["--request-timeout=5s", "--namespace", namespace]
        service = arguments.get("service")
        host = arguments.get("host")
        host = host.lower().rstrip(".") if host else None
        result["context"] = context
        result["host"] = host
        result["service"] = service
        evidence = {}
        missing = []
        resources = {
            "pods": ["-o", "wide"],
            "events": ["--sort-by=.metadata.creationTimestamp"],
            "services": ([service, "--ignore-not-found"] if service else []) + ["-o", f"custom-columns={SERVICE_COLUMNS}"],
            "endpointslices.discovery.k8s.io": (
                ["-l", f"kubernetes.io/service-name={service}"] if service else []
            ) + ["-o", f"custom-columns={SLICE_COLUMNS}"],
            "ingresses.networking.k8s.io": ["-o", "json"],
        }
        with ThreadPoolExecutor(max_workers=len(resources)) as workers:
            reads = {
                resource: workers.submit(_collect, query, resource, options, host, service)
                for resource, options in resources.items()
            }
            for resource, read in reads.items():
                try:
                    evidence[resource] = read.result()
                except (RuntimeError, TypeError, ValueError, KeyError):
                    missing.append(resource)

        result.update(
            status="OK" if not missing else "PARTIAL" if evidence else "UNAVAILABLE",
            evidence=evidence,
            unavailable_resources=missing,
        )
    except (RuntimeError, ValueError) as error:
        result.update(status="UNAVAILABLE", message=str(error))

    return json.dumps(result, ensure_ascii=False)
