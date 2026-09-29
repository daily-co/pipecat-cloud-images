"""FeatureManager: what an unavailable feature takes down with it."""

from feature_manager import FeatureKeys, FeatureManager, FeatureStatus


def test_an_unavailable_smallwebrtc_session_turns_off_what_needs_it():
    # app.py passes this when SmallWebRTC's session arguments do not build.
    # The transport, its ICE route and WhatsApp must go off through the
    # dependency, not by chance: each carries the dependency's reason, which
    # holds whether or not pipecat-ai's webrtc extra is installed.
    manager = FeatureManager(unavailable={FeatureKeys.SMALL_WEBRTC_SESSION: "does not build"})

    session = manager.features[FeatureKeys.SMALL_WEBRTC_SESSION]
    assert (session.status, session.error_message) == (FeatureStatus.DISABLED, "does not build")
    for key in (
        FeatureKeys.SMALLWEBRTC_TRANSPORT,
        FeatureKeys.SMALLWEBRTC_PATCH,
        FeatureKeys.WHATSAPP,
    ):
        feature = manager.features[key]
        assert feature.status == FeatureStatus.DISABLED
        assert feature.error_message == "Requires small_webrtc_session to be enabled"
    assert manager.is_enabled(FeatureKeys.DAILY_TRANSPORT)
    assert manager.is_enabled(FeatureKeys.WEBSOCKET_TRANSPORT)


def test_without_it_the_session_feature_is_detected():
    # The pipecatcloud every image ships has SmallWebRTC's session arguments.
    assert FeatureManager().is_enabled(FeatureKeys.SMALL_WEBRTC_SESSION)
