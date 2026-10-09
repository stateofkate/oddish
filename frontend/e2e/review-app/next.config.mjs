import path from "node:path";
import { docsBundle } from "../../docs-bundle.mjs";
export default {
  env: { ODDISH_DOCS_BUNDLE: docsBundle() },
  webpack(config) {
    config.resolve.alias["@clerk/nextjs"] = path.resolve("clerk.ts");
    return config;
  },
};
