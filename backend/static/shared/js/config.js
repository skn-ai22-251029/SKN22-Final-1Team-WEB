/**
 * config.js
 * 전역 설정 및 API 엔드포인트 관리
 * (보안을 위해 실제 운영 환경에서는 환경 변수 주입 방식으로 대체 권장)
 */

const CONFIG = {
    // API 베이스 URL
    API_BASE_URL: "http://localhost:8000/api/v1",
    
    // 분석 시뮬레이션 설정 (ms)
    SIMULATION_SPEED: 1.0, // 1.0은 기본 속도
    
    // 서비스 명칭
    SERVICE_NAME: "MirrAI (sAIon)",
    
    // 디버그 모드
    DEBUG: true
};

// Global export
window.MIRRAI_CONFIG = CONFIG;
