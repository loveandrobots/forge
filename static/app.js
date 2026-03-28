/* Forge Dashboard — htmx configuration and helpers */

(function () {
    "use strict";

    /* htmx configuration */
    document.body.addEventListener("htmx:configRequest", function (event) {
        event.detail.headers["Accept"] = "application/json";
    });

    /* Auto-scroll log feed to bottom on page load */
    var logFeed = document.getElementById("log-feed");
    if (logFeed) {
        logFeed.scrollTop = logFeed.scrollHeight;
    }
})();
