import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

const toUrl = text => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
function compile(path) {
    return ts.transpileModule(readFileSync(new URL(`../../frontend/src/${path}`, import.meta.url), "utf8"),
        { compilerOptions: { module: ts.ModuleKind.ESNext } }).outputText;
}
const { CollectionState } = await import(toUrl(compile("state/collectionState.ts")));
const controller = compile("controllers/view/collectionController.ts")
    .replace('"../../types"', JSON.stringify(toUrl(compile("types.ts"))))
    .replace('"../../utils"', JSON.stringify(toUrl(compile("utils.ts"))));
const { CollectionController } = await import(toUrl(controller));
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
function setup(apiClient) {
    const state = new CollectionState();
    state.directory = "photos";
    state.scanEnabled = true;
    const context = { apiClient, render() {}, state: { settings: { acceptedClassificationThreshold: .5 } } };
    const controller = new CollectionController(context, state, { clearResult() {} }, { reset() {} }, { closeEditor() {} }, {});
    controller.withSpeciesNames = async value => value;
    return { state, controller };
}
const snapshot = state => ({ state, total: 100, completed: 10, errors: 0, currentFile: "", message: "", species: [] });

test("Stop is sent before slow preference saving and stale polling cannot restart UI", async () => {
    const setting = deferred(), stop = deferred(), poll = deferred();
    let stopCalls = 0;
    const { state, controller } = setup({
        stopCollectionScan: () => { stopCalls++; return stop.promise; },
        setCollectionScanEnabled: () => setting.promise,
        collectionStatus: () => poll.promise,
    });
    state.startScan("job");
    const polling = controller.pollCollection("job");
    const stopping = controller.stopCollectionScan();
    assert.equal(stopCalls, 1);
    assert.equal(state.stopping, true);
    assert.equal(state.canScan(), false);
    await controller.stopCollectionScan();
    assert.equal(stopCalls, 1);
    stop.resolve(snapshot("stopped"));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(state.status.state, "stopped");
    poll.resolve(snapshot("running"));
    await polling;
    assert.equal(state.status.state, "stopped");
    assert.equal(state.activeJobId, null);
    setting.resolve({ collectionScanEnabled: false });
    await stopping;
});

test("double start makes one request and Stop cancels an in-flight launch", async () => {
    const launch = deferred();
    let launches = 0;
    const stopped = [];
    const { state, controller } = setup({
        startCollectionScan: () => { launches++; return launch.promise; },
        setCollectionScanEnabled: async value => ({ collectionScanEnabled: value }),
        stopCollectionScan: async id => { stopped.push(id); return snapshot("stopped"); },
    });
    const first = controller.runCollectionScanForRevision(null);
    await controller.runCollectionScanForRevision(null);
    assert.equal(launches, 1);
    assert.equal(state.starting, true);
    await controller.stopCollectionScan();
    assert.equal(state.stopping, true);
    launch.resolve({ jobId: "pending" });
    await first;
    assert.deepEqual(stopped, ["pending"]);
    assert.equal(state.activeJobId, null);
    assert.equal(state.starting, false);
    assert.equal(state.stopping, false);
    assert.equal(state.scanEnabled, false);
});

test("navigation cancels an in-flight launch without clearing auto-scan preference", async () => {
    const launch = deferred();
    const stopped = [];
    const { state, controller } = setup({
        startCollectionScan: () => launch.promise,
        stopCollectionScan: async id => { stopped.push(id); },
    });
    const first = controller.runCollectionScanForRevision(null);
    controller.stopForNavigation();
    launch.resolve({ jobId: "pending" });
    await first;
    assert.deepEqual(stopped, ["pending"]);
    assert.equal(state.activeJobId, null);
    assert.equal(state.scanEnabled, true);
});

test("a delayed Start preference response cannot undo Stop", async () => {
    const savingStart = deferred();
    let launches = 0;
    let settingsCalls = 0;
    const { state, controller } = setup({
        setCollectionScanEnabled: enabled => {
            settingsCalls++;
            return enabled ? savingStart.promise : Promise.resolve({ collectionScanEnabled: false });
        },
        startCollectionScan: async () => { launches++; return { jobId: "unexpected" }; },
    });
    const starting = controller.runCollectionScan();
    assert.equal(state.starting, true);
    assert.equal(state.canScan(), false);
    await controller.runCollectionScan();
    assert.equal(settingsCalls, 1);
    await controller.stopCollectionScan();
    savingStart.resolve({ collectionScanEnabled: true });
    await starting;
    assert.equal(launches, 0);
    assert.equal(state.scanEnabled, false);
    assert.equal(state.starting, false);
    assert.equal(state.stopping, false);
});
