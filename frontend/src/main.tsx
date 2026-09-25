import React from "react";
import ReactDOM from "react-dom/client";
/*
 * Inter — свой, а не системный (docs/16 §2.4).
 *
 * Команда работает на Windows, где Inter нет: без этого пакета шрифт молча
 * подменялся бы на Segoe UI, и вся выверенная типографика — размеры,
 * межстрочные, ширины колонок — считалась бы по другим метрикам. На macOS
 * подмена была бы на SF Pro, то есть у операторов и у того, кто принимает
 * работу, экраны выглядели бы по-разному.
 *
 * Пакет self-hosted: сервер стоит в России, и тянуть шрифт со стороннего CDN
 * значит поставить загрузку интерфейса в зависимость от чужой доступности.
 *
 * Берём ось насыщенности без курсива (`wght`, не `opsz`): курсив в интерфейсе
 * не используется, а оптическая ось удвоила бы вес файлов. Подмножества
 * объявлены все, но браузер по unicode-range скачивает только кириллицу и
 * латиницу — около 67 КБ.
 */
import "@fontsource-variable/inter/wght.css";

import "./app/mantine-styles";
import "./app/lc-vars.css";
// Строго после стилей Mantine: базовый слой их доводит и обязан их перебивать.
import "./app/lc-base.css";
import { App } from "./app/App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
