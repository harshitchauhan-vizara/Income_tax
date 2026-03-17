#!/usr/bin/env python3
"""
Test script for SarvamAI TTS service
"""

import asyncio
import os
import sys
from pathlib import Path

# Add the app directory to the Python path
app_dir = Path(__file__).parent
sys.path.insert(0, str(app_dir))

from app.config import Settings
from app.tts.sarvam_service import SarvamTTSService

async def test_sarvam_tts():
    """Test SarvamAI TTS service"""
    print("Testing SarvamAI TTS service...")
    
    # Load settings
    settings = Settings()
    print(f"SARVAM_API_KEY configured: {bool((settings.sarvam_api_key or '').strip())}")
    print(f"SARVAM_MODEL: {settings.sarvam_model}")
    print(f"SARVAM_SPEAKER: {settings.sarvam_speaker}")
    print(f"SARVAM_LANGUAGE_CODE: {settings.sarvam_language_code}")
    
    # Create TTS service
    tts_service = SarvamTTSService(settings)
    print(f"TTS service enabled: {tts_service.enabled}")
    
    if not tts_service.enabled:
        print("ERROR: TTS service is not enabled")
        return
    
    try:
        # Test client initialization
        client = tts_service._get_client()
        print("✓ SarvamAI client created successfully")
        
        # Test TTS request using the service method
        print("Testing TTS request...")
        audio_bytes = await tts_service.synthesize_sentence(
            text="Hello, this is a test of the SarvamAI TTS service.",
            language="en"
        )
        
        if audio_bytes:
            print("✓ TTS request successful")
            # Save the audio to a file for verification
            with open("test_output.wav", "wb") as f:
                f.write(audio_bytes)
            print(f"✓ Audio saved to test_output.wav ({len(audio_bytes)} bytes)")
        else:
            print("✗ No audio data returned")
            
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_sarvam_tts())