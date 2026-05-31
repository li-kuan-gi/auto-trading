const DEFAULT_GITHUB_OWNER = "li-kuan-gi";
const DEFAULT_GITHUB_REPO = "auto-trading";
const DEFAULT_WORKFLOW_FILE = "alpaca-fmp-swing-trader.yml";
const DEFAULT_GITHUB_REF = "main";
const DEFAULT_GITHUB_API_VERSION = "2026-03-10";

function required(env, name) {
  const value = env[name];
  if (!value) {
    throw new Error(`Missing required environment binding: ${name}`);
  }
  return value;
}

function optional(env, name, defaultValue) {
  return env[name] || defaultValue;
}

function jsonResponse(payload, init = {}) {
  return new Response(JSON.stringify(payload, null, 2), {
    ...init,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...(init.headers || {}),
    },
  });
}

async function parseResponse(response) {
  const text = await response.text();
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function dispatchWorkflow(env, trigger) {
  const owner = optional(env, "GITHUB_OWNER", DEFAULT_GITHUB_OWNER);
  const repo = optional(env, "GITHUB_REPO", DEFAULT_GITHUB_REPO);
  const workflowFile = optional(env, "GITHUB_WORKFLOW_FILE", DEFAULT_WORKFLOW_FILE);
  const ref = optional(env, "GITHUB_REF", DEFAULT_GITHUB_REF);
  const apiVersion = optional(env, "GITHUB_API_VERSION", DEFAULT_GITHUB_API_VERSION);
  const token = required(env, "GITHUB_TOKEN");

  const url = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/${workflowFile}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Accept": "application/vnd.github+json",
      "Authorization": `Bearer ${token}`,
      "Content-Type": "application/json",
      "User-Agent": "auto-trading-cloudflare-scheduler",
      "X-GitHub-Api-Version": apiVersion,
    },
    body: JSON.stringify({
      ref,
      return_run_details: true,
    }),
  });

  const payload = await parseResponse(response);
  const result = {
    ok: response.ok,
    status: response.status,
    trigger,
    workflow: `${owner}/${repo}/${workflowFile}`,
    ref,
    github_response: payload,
  };

  console.log(JSON.stringify(result));

  if (!response.ok) {
    throw new Error(`GitHub workflow dispatch failed: HTTP ${response.status} ${JSON.stringify(payload)}`);
  }

  return result;
}

export default {
  async scheduled(controller, env) {
    await dispatchWorkflow(env, {
      type: "cron",
      cron: controller.cron,
      scheduled_time: new Date(controller.scheduledTime).toISOString(),
    });
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return jsonResponse({ ok: true, service: "auto-trading-cloudflare-scheduler" });
    }

    return jsonResponse({ ok: false, error: "not_found" }, { status: 404 });
  },
};
