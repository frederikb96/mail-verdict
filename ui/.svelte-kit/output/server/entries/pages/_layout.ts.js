import { a as api } from "../../chunks/api.js";
const ssr = false;
async function load() {
  let accountList = [];
  let folderList = [];
  try {
    accountList = await api.accounts.list();
    if (accountList.length > 0) {
      folderList = await api.folders.list(accountList[0].id);
    }
  } catch {
  }
  return { accounts: accountList, folders: folderList };
}
export {
  load,
  ssr
};
