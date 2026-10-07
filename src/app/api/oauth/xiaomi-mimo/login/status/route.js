import { NextResponse } from "next/server";
import { sessionFromRequest, readSessionIdentity, attachSessionCookie, ensureServiceSession } from "@/lib/mimoLoginSession";
import { createDashboardAuthToken } from "@/lib/auth/dashboardSession";
import { createHash } from "node:crypto";

/**
 * GET /api/oauth/xiaomi-mimo/login/status?state=...
 * Polls the server-side login session (state lives in the httpOnly session
 * cookie — route handlers and the proxy don't share module memory). When a
 * passToken is in the jar, probes /api/user/xiaomi/me once to confirm the
 * session works, then returns the identity for the client to persist.
 */
export async function GET(request) {
  const url = new URL(request.url);
  const state = url.searchParams.get("state") || "";
  const sess = sessionFromRequest(request);
  if (!sess || (state && sess.state !== state)) {
    return NextResponse.json({ status: "expired" }, { status: 404 });
  }

  if (sess.status !== "done") {
    // AUTHORIZATION = passToken in the jar (captured during the proxied login
    // XHRs). No serviceToken exchange — weekly-quota API moved; re-wire later.
    if (readSessionIdentity(sess)) sess.status = "done";
  }

  if (sess.status !== "done") {
    // Re-arm the session cookie on every poll — the modal may sit on the login
    // form much longer than the 15min TTL, and only proxied responses used to
    // refresh it (browser silently drops an expired cookie before the POST).
    return attachSessionCookie(NextResponse.json({ status: "pending", region: sess.region }), sess);
  }

  const id = readSessionIdentity(sess);
  if (!id) {
    return attachSessionCookie(
      NextResponse.json({ status: "error", error: "session captured but passToken missing" }),
      sess,
    );
  }

  let reauthorizationProof;
  if (process.env.NINEROUTER_MANAGED_WORKER === "1") {
    if (!id.userId || !(await ensureServiceSession(sess))) {
      return NextResponse.json({ status: "error", error: "Provider reauthorization could not be verified" }, { status: 400 });
    }
    reauthorizationProof = await createDashboardAuthToken({
      purpose: "xiaomi-reauthorization", userId: id.userId, region: sess.region,
      credentialSha256: createHash("sha256").update(id.passToken).digest("hex"),
      reauthorizationExpiresAt: Date.now() + 60000,
    });
  }
  const payload = { status: "done", region: sess.region, ...id };
  // One-shot: don't let the identity linger past the client reading it.
  const res = NextResponse.json(payload);
  if (reauthorizationProof) res.cookies.set("9r_mimo_reauth", reauthorizationProof, {
    path: "/api/oauth/xiaomi-mimo/api-key", httpOnly: true, sameSite: "strict", maxAge: 60,
    secure: new URL(request.url).protocol === "https:",
  });
  res.cookies.set("9r_mimo_login", "", { path: "/", httpOnly: true, maxAge: 0 });
  return res;
}
