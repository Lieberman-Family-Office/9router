import { NextResponse } from "next/server";
import { killAppProcesses } from "@/lib/appUpdater";

// Shutdown app to release file locks for manual update
export async function POST() {
  if (process.env.NINEROUTER_MANAGED_WORKER === "1") {
    return NextResponse.json(
      { success: false, message: "Managed releases use 9router_deploy.py with a qualified tarball." },
      { status: 409 }
    );
  }

  try {
    await killAppProcesses();
  } catch { /* best effort */ }

  const response = NextResponse.json({ success: true, message: "Shutting down for manual update..." });

  setTimeout(() => process.exit(0), 500);

  return response;
}
