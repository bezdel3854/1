// Railway IaC template. Source is selected through the connected service.
// Run railway config plan against the intended project before applying.
import { defineRailway, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const data = volume("paper-data", { region: "us-west2", sizeMB: 1024 });
  const worker = service("bingx-paper-bot", {
    start: "python -m bingx_bot.cli paper",
    healthcheck: "/health",
    healthcheckTimeout: 180,
    replicas: 1,
    volumeMounts: { "/data": data },
    env: { MODE: "paper", STRATEGY: "candidate_120h", DB_PATH: "/data/candidate-120h.sqlite", PORT: "8080", RAILWAY_RUN_UID: "0" },
  });
  return project("bingx-paper-bot", { resources: [worker, data] });
});
