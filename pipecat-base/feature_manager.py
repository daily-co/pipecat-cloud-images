#!/usr/bin/env python

import inspect
from dataclasses import dataclass
from enum import Enum
from os import environ
from typing import Dict, List

from loguru import logger


class FeatureStatus(Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    ERROR = "error"
    MISSING_CONFIG = "missing_config"


class FeatureKeys(Enum):
    DAILY_TRANSPORT = "daily_transport"
    WEBSOCKET_TRANSPORT = "websocket_transport"
    SMALL_WEBRTC_SESSION = "small_webrtc_session"
    SMALLWEBRTC_TRANSPORT = "smallwebrtc_transport"
    SMALLWEBRTC_PATCH = "smallwebrtc_patch"
    WHATSAPP = "whatsapp"


@dataclass
class FeatureInfo:
    name: str
    status: FeatureStatus
    version_required: str = ""
    error_message: str = ""
    config_requirements: List[str] = None

    def __post_init__(self):
        if self.config_requirements is None:
            self.config_requirements = []


class FeatureManager:
    def __init__(self):
        self.features: Dict[FeatureKeys, FeatureInfo] = {}
        self._create_default_features()
        self._detect_features()

    def _create_default_features(self):
        """Create default features which are always enabled in the base image."""
        pipecat_features = [
            (FeatureKeys.DAILY_TRANSPORT, "Daily Transport", ""),
            (FeatureKeys.WEBSOCKET_TRANSPORT, "Websocket Transport", ""),
        ]
        for feature_key, feature_name, version_required in pipecat_features:
            self.features[feature_key] = self._create_feature_info(
                feature_key, feature_name, version_required, status=FeatureStatus.ENABLED
            )

    def _detect_features(self):
        """Detect all available features and their status."""
        self._detect_pipecatcloud_features()
        self._detect_smallwebrtc_features()

    def _create_feature_info(
        self,
        feature_key: FeatureKeys,
        name: str,
        version_required: str,
        status: FeatureStatus = FeatureStatus.ENABLED,
        error_message: str = "",
        config_requirements: List[str] = None,
    ) -> FeatureInfo:
        """Helper method to create FeatureInfo objects."""
        return FeatureInfo(
            name=name,
            status=status,
            version_required=version_required,
            error_message=error_message,
            config_requirements=config_requirements or [],
        )

    def _detect_pipecatcloud_features(self):
        """Detect PipecatCloud features."""
        feature_key = FeatureKeys.SMALL_WEBRTC_SESSION
        feature_name = "SmallWebRTC Session Arguments"
        version_required = "pipecatcloud>=0.2.5"

        try:
            from pipecatcloud.agent import SmallWebRTCSessionArguments

            self.features[feature_key] = self._create_feature_info(
                feature_key, feature_name, version_required
            )
        except ImportError as e:
            self.features[feature_key] = self._create_feature_info(
                feature_key,
                feature_name,
                version_required,
                status=FeatureStatus.DISABLED,
                error_message=str(e),
            )

    def _detect_smallwebrtc_features(self):
        """Detect Pipecat AI features."""
        # Only enable pipecat features if small_webrtc_session is enabled
        if not self.is_enabled(FeatureKeys.SMALL_WEBRTC_SESSION):
            reason = "Requires small_webrtc_session to be enabled"
            self._create_disabled_smallwebrtc_features(reason)
            self._create_disabled_smallwebrtc_patch_features(reason)
            self._create_disabled_whatsapp_feature(reason)
            return

        self._detect_smallwebrtc_transport()
        if not self.is_enabled(FeatureKeys.SMALLWEBRTC_TRANSPORT):
            reason = "Requires SmallWebRTCTransport to be enabled"
            self._create_disabled_smallwebrtc_patch_features(reason)
            self._create_disabled_whatsapp_feature(reason)
            return

        self._detect_smallwebrtc_patch()
        self._detect_whatsapp_features()

    def _detect_smallwebrtc_transport(self):
        """Detect SmallWebRTC Transport feature."""
        feature_key = FeatureKeys.SMALLWEBRTC_TRANSPORT
        feature_name = "SmallWebRTC Transport"
        version_required = "pipecat-ai[webrtc]"

        try:
            from aiortc import MediaStreamTrack
            from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
            from pipecat.transports.smallwebrtc.request_handler import (
                ConnectionMode,
                SmallWebRTCRequest,
                SmallWebRTCRequestHandler,
            )

            self.features[feature_key] = self._create_feature_info(
                feature_key, feature_name, version_required
            )
        except ImportError as e:
            self.features[feature_key] = self._create_feature_info(
                feature_key,
                feature_name,
                version_required,
                status=FeatureStatus.DISABLED,
                error_message=str(e),
            )
        except Exception as e:
            self.features[feature_key] = self._create_feature_info(
                feature_key,
                feature_name,
                version_required,
                status=FeatureStatus.ERROR,
                error_message=str(e),
            )

    def _detect_smallwebrtc_patch(self):
        """Detect SmallWebRTC Patch feature."""
        feature_key = FeatureKeys.SMALLWEBRTC_PATCH
        feature_name = "SmallWebRTC ICE Candidates"
        version_required = "pipecat-ai>=0.0.91"

        try:
            from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCPatchRequest

            self.features[feature_key] = self._create_feature_info(
                feature_key, feature_name, version_required
            )
        except ImportError as e:
            self.features[feature_key] = self._create_feature_info(
                feature_key,
                feature_name,
                version_required,
                status=FeatureStatus.DISABLED,
                error_message=str(e),
            )

    def _create_disabled_smallwebrtc_features(self, reason: str):
        """Create disabled pipecat features when dependencies are not met."""
        feature_key = FeatureKeys.SMALLWEBRTC_TRANSPORT
        feature_name = "SmallWebRTC Transport"
        version_required = "pipecat-ai[webrtc]"

        self.features[feature_key] = self._create_feature_info(
            feature_key,
            feature_name,
            version_required,
            status=FeatureStatus.DISABLED,
            error_message=reason,
        )

    def _create_disabled_smallwebrtc_patch_features(self, reason: str):
        """Create disabled pipecat features when dependencies are not met."""
        feature_key = FeatureKeys.SMALLWEBRTC_PATCH
        feature_name = "SmallWebRTC ICE Candidates"
        version_required = "pipecat-ai>=0.0.91"

        self.features[feature_key] = self._create_feature_info(
            feature_key,
            feature_name,
            version_required,
            status=FeatureStatus.DISABLED,
            error_message=reason,
        )

    def _detect_whatsapp_features(self):
        """Detect WhatsApp features."""
        feature_key = FeatureKeys.WHATSAPP
        feature_name = "WhatsApp Integration"
        version_required = "pipecat-ai>=0.0.89"
        required_env_vars = [
            "WHATSAPP_TOKEN",
            "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_APP_SECRET",
        ]

        try:
            from pipecat.transports.whatsapp.api import WhatsAppWebhookRequest
            from pipecat.transports.whatsapp.client import WhatsAppClient

            # Check if WhatsAppClient supports the secret parameter
            signature = inspect.signature(WhatsAppClient)
            if "whatsapp_secret" not in signature.parameters:
                raise Exception("WhatsApp client doesn't support whatsapp_secret parameter")

            # Check environment variables
            missing_vars = [var for var in required_env_vars if not environ.get(var)]

            if missing_vars:
                self.features[feature_key] = self._create_feature_info(
                    feature_key,
                    feature_name,
                    version_required,
                    status=FeatureStatus.MISSING_CONFIG,
                    config_requirements=required_env_vars,
                    error_message=f"Missing environment variables: {', '.join(missing_vars)}",
                )
            else:
                self.features[feature_key] = self._create_feature_info(
                    feature_key,
                    feature_name,
                    version_required,
                    config_requirements=required_env_vars,
                )

        except ImportError as e:
            self.features[feature_key] = self._create_feature_info(
                feature_key,
                feature_name,
                version_required,
                status=FeatureStatus.DISABLED,
                error_message=str(e),
            )
        except Exception as e:
            self.features[feature_key] = self._create_feature_info(
                feature_key,
                feature_name,
                version_required,
                status=FeatureStatus.ERROR,
                error_message=str(e),
            )

    def _create_disabled_whatsapp_feature(self, reason: str):
        """Create disabled WhatsApp feature when dependencies are not met."""
        feature_key = FeatureKeys.WHATSAPP
        feature_name = "WhatsApp Integration"
        version_required = "pipecat-ai>=0.0.89"

        self.features[feature_key] = self._create_feature_info(
            feature_key,
            feature_name,
            version_required,
            status=FeatureStatus.DISABLED,
            error_message=reason,
        )

    def is_enabled(self, feature: FeatureKeys) -> bool:
        """Check if a feature is enabled."""
        feature = self.features.get(feature)
        return feature is not None and feature.status == FeatureStatus.ENABLED

    def log_features_summary(self):
        """Log a summary of all features and their status."""
        logger.info("=" * 60)
        logger.info("Features available in this base image: ")
        logger.info("=" * 60)

        for feature_name, feature_info in self.features.items():
            status_emoji = {
                FeatureStatus.ENABLED: "✅",
                FeatureStatus.DISABLED: "◻️",
                FeatureStatus.ERROR: "❌",
                FeatureStatus.MISSING_CONFIG: "⚠️",
            }.get(feature_info.status, "❓")

            logger.info(f"{status_emoji} {feature_info.name}: {feature_info.status.value.upper()}")

            if feature_info.status == FeatureStatus.ENABLED:
                # There is nothing else to print here in this case.
                continue

            if feature_info.version_required:
                logger.info(f"   Required: {feature_info.version_required}")

            if feature_info.config_requirements:
                logger.info(f"   Config needed: {', '.join(feature_info.config_requirements)}")

            if feature_info.error_message and feature_info.status != FeatureStatus.ENABLED:
                logger.info(f"   Reason: {feature_info.error_message}")

        logger.info("=" * 60)
