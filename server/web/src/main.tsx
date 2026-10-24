import { render } from "preact";
import "@fontsource-variable/inter";
import { BRAND } from "./brand";
import { App } from "./components/App";
import "./styles.css";

document.title = BRAND.name;
render(<App />, document.getElementById("app")!);
