use serde_json::{json, Value};

pub fn normalize_line(line: &str) -> Option<String> {
    let trimmed = line.trim();
    if trimmed.is_empty() {
        return None;
    }

    let payload = match serde_json::from_str::<Value>(trimmed) {
        Ok(value) => value,
        Err(_) => return Some(json!({"event_type": "log", "message": trimmed}).to_string()),
    };

    let event_type = payload.get("type").and_then(Value::as_str);
    match event_type {
        Some("thread.started") => {
            let thread_id = payload.get("thread_id").and_then(Value::as_str)?;
            Some(json!({"event_type": "session", "thread_id": thread_id}).to_string())
        }
        Some("agent.updated") => {
            let status = payload.get("status").and_then(Value::as_str).unwrap_or_default();
            let message = payload.get("message").and_then(Value::as_str).unwrap_or_default();
            Some(json!({"event_type": "status", "status": status, "message": message}).to_string())
        }
        Some("response.output_text.delta") => {
            let message = payload.get("delta").and_then(Value::as_str)?;
            Some(json!({"event_type": "text_delta", "message": message}).to_string())
        }
        Some("response.output_text.done") => {
            let message = payload.get("text").and_then(Value::as_str)?;
            Some(json!({"event_type": "text_done", "message": message}).to_string())
        }
        Some("turn.completed") => Some(json!({"event_type": "done"}).to_string()),
        Some(_) => Some(trimmed.to_string()),
        None => Some(json!({"event_type": "log", "message": trimmed}).to_string()),
    }
}
