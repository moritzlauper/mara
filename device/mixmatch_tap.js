// MixMatch Tap: adds the track's identity to each measurement frame and
// passes it on to udpsend as an OSC message. Runs inside Max for Live.
autowatch = 1;
inlets = 1;
outlets = 1;

var trackId = 0;
var trackName = "";
var kind = "track";
var frame = 0;

function init() {
    var api = new LiveAPI("this_device canonical_parent");
    trackId = parseInt(api.id, 10);
    var path = String(api.unquotedpath);
    kind = path.indexOf("master_track") >= 0 ? "master" : (path.indexOf("return_tracks") >= 0 ? "return" : "track");
    refreshName(api);
    // Also update instances already saved in a Live set with the old presentation label.
    this.patcher.apply(function (obj) {
        if (obj.maxclass === "comment" && String(obj.getattr("text")) === "MixMatch") {
            obj.message("set", "MARA");
        }
    });
}

function refreshName(api) {
    api = api || new LiveAPI("this_device canonical_parent");
    var name = api.get("name");
    trackName = name instanceof Array ? name.join(" ") : String(name);
    outlet(0, ["/mm/name", trackId, kind, trackName]);
}

// One frame from [pack]: m_rms s_rms s_low_rms peak_l peak_r band1 … band31
function list() {
    // autowatch can recompile a running instance without another live.thisdevice bang.
    if (!trackId) init();
    frame++;
    if (frame % 20 === 0) refreshName();
    outlet(0, ["/mm/tap", trackId].concat(arrayfromargs(arguments)));
}
