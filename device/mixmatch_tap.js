// MixMatch Tap: adds the track's identity to each measurement frame and
// passes it on to udpsend as an OSC message. Runs inside Max for Live.
//
// Identity is the device's own Live id, so two taps on one track (Main has a
// second one in front of clipper and limiter) never mix their frames. On Main,
// a tap with another tap after it reports kind «master_in»: the level that
// enters the loudness chain. The last tap reports «master» as before.
autowatch = 1;
inlets = 1;
outlets = 1;

var TAP = "MixMatch Tap";
var tapId = 0;
var deviceId = 0;
var trackName = "";
var kind = "track";
var frame = 0;

function asString(v) {
    return v instanceof Array ? v.join(" ") : String(v);
}

function init() {
    var me = new LiveAPI("this_device");
    deviceId = parseInt(me.id, 10);
    var api = new LiveAPI("this_device canonical_parent");
    tapId = deviceId || parseInt(api.id, 10);
    refreshName(api);
    // Also update instances already saved in a Live set with the old presentation label.
    this.patcher.apply(function (obj) {
        if (obj.maxclass === "comment" && String(obj.getattr("text")) === "MixMatch") {
            obj.message("set", "MARA");
        }
    });
}

// True when another MixMatch Tap follows this one in the same chain.
function tapAfterMe(api) {
    var ids = api.get("devices");   // ["id", 3, "id", 7, …]
    var seen = false;
    for (var i = 1; i < ids.length; i += 2) {
        var id = parseInt(ids[i], 10);
        if (id === deviceId) {
            seen = true;
        } else if (seen && asString(new LiveAPI("id " + id).get("name")) === TAP) {
            return true;
        }
    }
    return false;
}

function detectKind(api) {
    var path = String(api.unquotedpath);
    if (path.indexOf("master_track") >= 0) {
        return tapAfterMe(api) ? "master_in" : "master";
    }
    return path.indexOf("return_tracks") >= 0 ? "return" : "track";
}

function refreshName(api) {
    api = api || new LiveAPI("this_device canonical_parent");
    trackName = asString(api.get("name"));
    kind = detectKind(api);
    outlet(0, ["/mm/name", tapId, kind, trackName]);
}

// One frame from [pack]: m_rms s_rms s_low_rms peak_l peak_r band1 … band31
function list() {
    // autowatch can recompile a running instance without another live.thisdevice bang.
    if (!tapId) init();
    frame++;
    if (frame % 20 === 0) refreshName();
    outlet(0, ["/mm/tap", tapId].concat(arrayfromargs(arguments)));
}
