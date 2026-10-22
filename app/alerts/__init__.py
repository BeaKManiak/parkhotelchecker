"""Alert selection and delivery (Telegram).

The split inside this package is deliberate:

- ``digest``   decides which price moves are worth a line, and renders them;
- ``failures`` decides when silence itself is the emergency;
- ``telegram`` is the transport, and knows nothing about prices;
- ``notifier`` composes the two sources and survives either one failing.
"""
