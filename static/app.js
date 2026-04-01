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

    /* Preserve kanban scroll position across htmx refreshes */
    var kanbanScrollLeft = 0;

    document.body.addEventListener("htmx:beforeSwap", function (event) {
        var kanban = document.querySelector(".kanban");
        if (kanban) {
            kanbanScrollLeft = kanban.scrollLeft;
        }
    });

    document.body.addEventListener("htmx:afterSettle", function (event) {
        if (kanbanScrollLeft > 0) {
            var kanban = document.querySelector(".kanban");
            if (kanban) {
                requestAnimationFrame(function () {
                    kanban.scrollLeft = kanbanScrollLeft;
                });
            }
        }
    });
})();
