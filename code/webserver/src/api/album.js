/* ============================================================
 * 文件夹浏览（P-11 · R16）的接口封装与契约常量
 * ============================================================
 * 薄封装：只做「路径 + 参数名」的转写，不做业务加工
 * （与 api/place.js / api/browse.js 同一风格）。
 *
 * 两件事放在这里而不是页面里
 * -------------------------
 *   ① `dirPathParam()` —— 把目录键翻译成 `dirPath` 入参。
 *      ⚠️ 它必须处理根目录：`api/request.js` 的 `paramsSerializer` 会丢弃空串，
 *        传 `dirPath: ''` 时**参数根本不会发出**，后端理解成"不筛选"
 *        ⇒ 返回**全库照片**（用户以为进了根目录，实际看到全库，且不报错）。
 *      所以根目录统一换成保留 token（见 utils/photoReturn.js 的 ALBUM_ROOT_TOKEN，
 *      那里也是 scope 载荷用的同一个 token —— 三处共用一个，不许再写第二份）。
 *   ② 树与照片各一个请求，页面只调这两个函数。
 *
 * ⚠️ 目录树的**排序**在服务端做（`processor/album/dirPath.nameSortKey`，
 *   数字感知：`2013.7.1` 排在 `2013.10.01` 前面）。这里**不重复实现** ——
 *   两处各排一遍必然漂移，而症状是"同一批目录在两个入口里顺序不同"。
 * ============================================================ */
import { ALBUM_ROOT_TOKEN } from '@/utils/photoReturn'
import request from './request'

export { ALBUM_ROOT_TOKEN }

/**
 * 目录键 -> `dirPath` 入参（根目录换成保留 token，**绝不传空串**）。
 *
 * ⚠️ 与 `ALBUM_ROOT_TOKEN` 的契约：后端把 token 归一成空串再比较，
 *   而空串表示根目录。所以这里返回的**永远是非空字符串**。
 */
export function dirPathParam(dirKey) {
  return String(dirKey || '') || ALBUM_ROOT_TOKEN
}

/**
 * 目录树 + 当前位置（**只读**）。
 *
 * 返回：`{ ok, path, pathName, pathExists, breadcrumb[], root, directCount,
 *          subDirCount, directTotalCount, photoTotal, dirTotal, elapsedMs, note }`
 *   · `root` 是**整棵树**（嵌套，每个节点带 `count` 直属 / `totalCount` 递归），
 *     展开/折叠纯前端做，切换目录不会让树重新加载、展开状态也不会丢；
 *   · `breadcrumb` 的每一段都可点击跳回。
 *
 * @param {{ path?: string }} params `path` = 目录键；根目录传保留 token
 */
export function listAlbumPath(params = {}) {
  return request.get('/albums/path', { params })
}
