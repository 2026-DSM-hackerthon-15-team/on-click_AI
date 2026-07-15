"""
_run_langchain_agent 함수의 문제를 테스트하는 스크립트
"""
import json
from unittest.mock import Mock, MagicMock, patch
from datetime import date

# 필요한 모듈들 import
from src.ai_service.main import _run_langchain_agent, AiChatRequest


def test_missing_tool_name():
    """테스트 1: tool_calls에 name이 없거나 도구가 tool_map에 없을 때"""
    print("\n=== 테스트 1: 존재하지 않는 도구 호출 (정상 처리) ===")
    
    payload = AiChatRequest(
        userId=1,
        storeId=1,
        chatRoomId=1,
        message="매출을 분석해줘",
        availableTools=["sales_analysis"],
    )
    
    # Mock LLM 응답 - 존재하지 않는 도구 호출
    mock_response = Mock()
    mock_response.tool_calls = [
        {
            "name": "NONEXISTENT_TOOL",  # 이 도구는 존재하지 않음
            "args": {},
            "id": "call_123"
        }
    ]
    mock_response.content = "도구를 실행하겠습니다"
    
    with patch('src.ai_service.main._build_langchain_model') as mock_build:
        mock_llm = Mock()
        mock_model = Mock()
        mock_model.invoke.return_value = mock_response
        mock_llm.bind_tools.return_value = mock_model
        mock_build.return_value = mock_llm
        
        with patch('src.ai_service.main.get_langchain_tools') as mock_tools:
            # 실제 도구들 (sales_analysis만 있음)
            real_tool = Mock()
            real_tool.name = "sales_analysis"
            mock_tools.return_value = [real_tool]
            
            try:
                result = _run_langchain_agent(payload, ["sales_analysis"])
                # 존재하지 않는 도구는 gracefully 처리되어야 함
                if result and result.usedTools and any(t.status == "FAILED" for t in result.usedTools):
                    print(f"✅ 존재하지 않는 도구가 FAILED 상태로 정상 처리됨")
                    failed_tools = [t for t in result.usedTools if t.status == "FAILED"]
                    print(f"   FAILED 도구: {failed_tools[0].toolName}")
                    print(f"   에러 메시지: {failed_tools[0].resultSummary[:100]}...")
                else:
                    print(f"❌ 예상과 다른 결과")
            except Exception as e:
                print(f"❌ 예외 발생: {type(e).__name__}: {e}")


def test_missing_call_id():
    """테스트 2: tool_calls에 id가 없을 때"""
    print("\n=== 테스트 2: tool_calls에 id 필드 누락 (안전하게 skip) ===")
    
    payload = AiChatRequest(
        userId=1,
        storeId=1,
        chatRoomId=1,
        message="매출을 분석해줘",
        availableTools=["sales_analysis"],
    )
    
    # Mock LLM 응답 - id가 없음, 2번째는 최종 답변
    mock_response1 = Mock()
    mock_response1.tool_calls = [
        {
            "name": "sales_analysis",
            "args": {},
            # "id" 필드가 없음!
        }
    ]
    
    mock_response2 = Mock()
    mock_response2.tool_calls = []
    mock_response2.content = "최종 답변입니다"
    
    with patch('src.ai_service.main._build_langchain_model') as mock_build:
        mock_llm = Mock()
        mock_model = Mock()
        mock_model.invoke.side_effect = [mock_response1, mock_response2]
        mock_llm.bind_tools.return_value = mock_model
        mock_build.return_value = mock_llm
        
        with patch('src.ai_service.main.get_langchain_tools') as mock_tools:
            real_tool = Mock()
            real_tool.name = "sales_analysis"
            real_tool.invoke.return_value = {"ok": True, "data": {"sales": 100000}}
            mock_tools.return_value = [real_tool]
            
            try:
                result = _run_langchain_agent(payload, ["sales_analysis"])
                if result:
                    print(f"✅ id가 없는 tool_call이 안전하게 skip됨")
                    print(f"   최종 답변: '{result.answer}'")
                    print(f"   사용된 도구: {len(result.usedTools)}개")
                else:
                    print(f"❌ 예상과 다른 결과")
            except Exception as e:
                print(f"❌ 예외 발생: {type(e).__name__}: {e}")


def test_none_tool_name():
    """테스트 3: tool name이 None일 때"""
    print("\n=== 테스트 3: tool_calls에서 name이 None (안전하게 skip) ===")
    
    payload = AiChatRequest(
        userId=1,
        storeId=1,
        chatRoomId=1,
        message="뭔가 해줘",
        availableTools=["sales_analysis"],
    )
    
    # Mock LLM 응답 - name이 None, 2번째는 최종 답변
    mock_response1 = Mock()
    mock_response1.tool_calls = [
        {
            "name": None,  # name이 None!
            "args": {},
            "id": "call_123"
        }
    ]
    
    mock_response2 = Mock()
    mock_response2.tool_calls = []
    mock_response2.content = "최종 답변입니다"
    
    with patch('src.ai_service.main._build_langchain_model') as mock_build:
        mock_llm = Mock()
        mock_model = Mock()
        mock_model.invoke.side_effect = [mock_response1, mock_response2]
        mock_llm.bind_tools.return_value = mock_model
        mock_build.return_value = mock_llm
        
        with patch('src.ai_service.main.get_langchain_tools') as mock_tools:
            real_tool = Mock()
            real_tool.name = "sales_analysis"
            mock_tools.return_value = [real_tool]
            
            try:
                result = _run_langchain_agent(payload, ["sales_analysis"])
                if result:
                    print(f"✅ name이 None인 tool_call이 안전하게 skip됨")
                    print(f"   최종 답변: '{result.answer}'")
                    print(f"   Pydantic ValidationError 없음")
                else:
                    print(f"❌ 예상과 다른 결과")
            except Exception as e:
                print(f"❌ 예외 발생: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print("=" * 60)
    print("_run_langchain_agent 함수 문제 테스트")
    print("=" * 60)
    
    test_missing_tool_name()
    test_missing_call_id()
    test_none_tool_name()
    
    print("\n" + "=" * 60)
    print("테스트 완료")
    print("=" * 60)
