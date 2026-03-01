import { a as api } from "../../../../chunks/api.js";
import { error } from "@sveltejs/kit";
const load = async ({ params, url }) => {
  const accountId = url.searchParams.get("account_id");
  if (!accountId) {
    error(400, "Missing account_id parameter");
  }
  try {
    const mail = await api.mails.get(params.id, accountId);
    return { mail };
  } catch (e) {
    error(404, "Mail not found");
  }
};
export {
  load
};
