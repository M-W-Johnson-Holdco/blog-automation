/**
 * Cloudflare Worker: verify Slack Events API requests and dispatch GitHub Actions.
 *
 * One Worker serves all companies:
 *   POST /slack/events/peachtree
 *   POST /slack/events/tc
 * Cron dispatches weekly.yml with company=both.
 */

const VALID_COMPANIES = ["peachtree", "tc"];

function timingSafeEqual(a, b) {
  if (a.length !== b.length) {
    return false;
  }
  let out = 0;
  for (let i = 0; i < a.length; i += 1) {
    out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return out === 0;
}

async function verifySlackRequest(request, signingSecret) {
  const timestamp = request.headers.get("x-slack-request-timestamp") || "";
  const signature = request.headers.get("x-slack-signature") || "";
  if (!timestamp || !signature) {
    throw new Error("Missing Slack signature headers.");
  }

  const skew = Math.abs(Date.now() / 1000 - Number(timestamp));
  if (!Number.isFinite(skew) || skew > 60 * 5) {
    throw new Error("Slack request timestamp is too old.");
  }

  const body = await request.text();
  const base = `v0:${timestamp}:${body}`;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(signingSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const digest = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(base));
  const hex = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  const expected = `v0=${hex}`;
  if (!timingSafeEqual(expected, signature)) {
    throw new Error("Invalid Slack signature.");
  }

  return { body, payload: JSON.parse(body) };
}

function companyFromPath(pathname) {
  const match = String(pathname || "").match(/^\/slack\/events\/([a-z0-9_-]+)\/?$/i);
  if (!match) {
    return "";
  }
  const company = match[1].toLowerCase();
  return VALID_COMPANIES.includes(company) ? company : "";
}

function companyContext(company, env) {
  const slug = String(company || "").trim().toLowerCase();
  if (!VALID_COMPANIES.includes(slug)) {
    throw new Error(`Unknown company "${company}". Expected one of: ${VALID_COMPANIES.join(", ")}.`);
  }
  const secretKey = `SLACK_SIGNING_SECRET_${slug.toUpperCase()}`;
  const botKey = `SLACK_BOT_USER_ID_${slug.toUpperCase()}`;
  return {
    company: slug,
    signingSecret: String(env[secretKey] || "").trim(),
    botUserId: String(env[botKey] || "").trim(),
    secretKey,
  };
}

function isConfiguredBotUser(event, botUserId) {
  return Boolean(botUserId && event && event.user === botUserId);
}

function isThreadReply(event) {
  return Boolean(event.thread_ts && event.ts && event.thread_ts !== event.ts);
}

function isTopLevelMessage(event) {
  return !event.thread_ts;
}

function stripSlackMentions(text) {
  return String(text || "").replace(/<@[^>]+>/g, "").trim();
}

function isPipelineMentionCommand(event) {
  return event.type === "app_mention" && isTopLevelMessage(event) && stripSlackMentions(event.text).toLowerCase() === "pipeline";
}

function isRepeatPipelineReaction(event) {
  return event.type === "reaction_added" && event.reaction === "repeat" && event.item && event.item.type === "message";
}

function shouldForwardEvent(event, botUserId = "") {
  if (!event || typeof event !== "object") {
    return false;
  }
  if (event.bot_id) {
    return false;
  }
  if (["bot_message", "message_changed", "message_deleted"].includes(event.subtype)) {
    return false;
  }

  if (isConfiguredBotUser(event, botUserId)) {
    return false;
  }

  if (event.type === "reaction_added" && event.reaction === "repeat") {
    return false;
  }

  if (["reaction_added", "reaction_removed"].includes(event.type)) {
    return true;
  }

  if (event.type === "message") {
    return isThreadReply(event);
  }

  return false;
}

function encodeEventB64(event) {
  const bytes = new TextEncoder().encode(JSON.stringify(event));
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

async function dispatchGithubWorkflow(env, workflowFile, inputs = {}) {
  const token = env.GITHUB_TOKEN;
  const repo = env.GITHUB_REPOSITORY;
  const ref = env.GITHUB_REF_NAME || "main";
  if (!token || !repo) {
    throw new Error("GITHUB_TOKEN and GITHUB_REPOSITORY must be configured on the Worker.");
  }

  const response = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/${workflowFile}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "blog-automation-slack-worker",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      ref,
      inputs,
    }),
  });

  if (response.status !== 204) {
    const detail = await response.text();
    throw new Error(`GitHub dispatch failed for ${workflowFile}: HTTP ${response.status} ${detail}`);
  }
}

async function dispatchSlackApproveWorkflow(env, company, event, eventId) {
  await dispatchGithubWorkflow(env, "slack_approve.yml", {
    event_b64: encodeEventB64(event),
    event_id: eventId || `${event.type}-${Date.now()}`,
    company,
  });
}

async function dispatchWeeklyPipelineWorkflow(env, company, inputs = {}) {
  await dispatchGithubWorkflow(env, "weekly.yml", {
    send_to_slack: "true",
    company,
    ...inputs,
  });
}

function scheduledDateParts(scheduledTime) {
  const date = new Date(scheduledTime);
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  return Object.fromEntries(parts.map((part) => [part.type, part.value]));
}

// Returns "dispatch" for a normal scheduled slot, "retry" for the Monday
// noon fallback slot, or null if this tick shouldn't do anything.
function weeklyScheduleAction(scheduledTime) {
  const parts = scheduledDateParts(scheduledTime);
  if (parts.minute !== "00") {
    return null;
  }
  if (parts.weekday === "Wed" && parts.hour === "08") {
    return "dispatch";
  }
  if (parts.weekday === "Mon" && parts.hour === "08") {
    return "dispatch";
  }
  // Monday noon ET retry in case the 8 AM dispatch never reached GitHub
  // Actions (Worker/GitHub outage, missed cron tick, etc.).
  if (parts.weekday === "Mon" && parts.hour === "12") {
    return "retry";
  }
  return null;
}

function scheduledDayInput(scheduledTime) {
  const parts = scheduledDateParts(scheduledTime);
  if (parts.minute !== "00") {
    return "";
  }
  if (parts.weekday === "Mon" && (parts.hour === "08" || parts.hour === "12")) {
    return "monday";
  }
  if (parts.weekday === "Wed" && parts.hour === "08") {
    return "wednesday";
  }
  return "";
}

function utcDateOnly(scheduledTime) {
  return new Date(scheduledTime).toISOString().slice(0, 10);
}

async function hasWeeklyRunToday(env, scheduledTime) {
  const token = env.GITHUB_TOKEN;
  const repo = env.GITHUB_REPOSITORY;
  if (!token || !repo) {
    throw new Error("GITHUB_TOKEN and GITHUB_REPOSITORY must be configured on the Worker.");
  }

  const response = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/weekly.yml/runs?per_page=20`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "blog-automation-slack-worker",
        "X-GitHub-Api-Version": "2022-11-28",
      },
    },
  );

  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`GitHub runs lookup failed: HTTP ${response.status} ${detail}`);
  }

  const data = await response.json();
  const today = utcDateOnly(scheduledTime);
  const runs = Array.isArray(data.workflow_runs) ? data.workflow_runs : [];
  return runs.some((run) => String(run.created_at || "").slice(0, 10) === today);
}

export default {
  async scheduled(event, env, ctx) {
    const action = weeklyScheduleAction(event.scheduledTime);
    const scheduledDay = scheduledDayInput(event.scheduledTime);
    if (!action || !scheduledDay) {
      return;
    }

    // One cron tick runs both companies via weekly.yml's matrix + company=both.
    if (action === "retry") {
      ctx.waitUntil(
        hasWeeklyRunToday(env, event.scheduledTime)
          .then((alreadyRan) => {
            if (alreadyRan) {
              return;
            }
            return dispatchWeeklyPipelineWorkflow(env, "both", { scheduled_day: scheduledDay });
          })
          .catch((error) => {
            console.error("scheduled pipeline retry dispatch failed", error);
          }),
      );
      return;
    }

    ctx.waitUntil(
      dispatchWeeklyPipelineWorkflow(env, "both", { scheduled_day: scheduledDay }).catch((error) => {
        console.error("scheduled pipeline dispatch failed", error);
      }),
    );
  },

  async fetch(request, env, ctx) {
    if (request.method === "GET") {
      const url = new URL(request.url);
      if (url.pathname === "/health") {
        return Response.json({
          status: "ok",
          companies: VALID_COMPANIES,
          event_paths: VALID_COMPANIES.map((company) => `/slack/events/${company}`),
        });
      }
      return new Response("Blog Automation Slack Events Worker", { status: 200 });
    }

    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const url = new URL(request.url);
    const company = companyFromPath(url.pathname);
    if (!company) {
      return new Response(
        `Not Found. Use one of: ${VALID_COMPANIES.map((slug) => `/slack/events/${slug}`).join(", ")}`,
        { status: 404 },
      );
    }

    let context;
    try {
      context = companyContext(company, env);
    } catch (error) {
      return new Response(String(error.message || error), { status: 404 });
    }

    if (!context.signingSecret) {
      return new Response(`${context.secretKey} is not configured.`, { status: 500 });
    }

    let payload;
    try {
      ({ payload } = await verifySlackRequest(request, context.signingSecret));
    } catch (error) {
      return new Response(String(error.message || error), { status: 401 });
    }

    if (payload.type === "url_verification") {
      return Response.json({ challenge: payload.challenge });
    }

    if (payload.type === "event_callback") {
      const event = payload.event || {};
      if (isConfiguredBotUser(event, context.botUserId)) {
        return Response.json({ ok: true });
      }
      if (isPipelineMentionCommand(event) || isRepeatPipelineReaction(event)) {
        ctx.waitUntil(
          dispatchWeeklyPipelineWorkflow(env, context.company).catch((error) => {
            console.error("pipeline dispatch failed", error);
          }),
        );
      } else if (shouldForwardEvent(event, context.botUserId)) {
        ctx.waitUntil(
          dispatchSlackApproveWorkflow(env, context.company, event, payload.event_id || "").catch((error) => {
            console.error("dispatch failed", error);
          }),
        );
      }
      return Response.json({ ok: true });
    }

    return Response.json({ ok: true });
  },
};
