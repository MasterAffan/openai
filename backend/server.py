from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from services.storage_service import StorageService
from services.vertex_service import VertexService
from services.job_service import JobService
from services.supabase_service import SupabaseService
from services.video_merge_service import VideoMergeService
from utils.env import settings
from typing import Optional
import traceback

# Initialize services
storage_service = StorageService()
vertex_service = VertexService()
job_service = JobService(vertex_service)
supabase_service = SupabaseService()
video_merge_service = VideoMergeService(storage_service)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("🚀 FlowBoard API starting...")
    print(f"   Project: {settings.GOOGLE_CLOUD_PROJECT}")
    print(f"   Location: {settings.GOOGLE_CLOUD_LOCATION}")
    yield
    # Shutdown
    print("👋 FlowBoard API shutting down...")

app = FastAPI(
    title="FlowBoard API",
    description="Video generation API powered by Vertex AI",
    version="1.0.0",
    lifespan=lifespan
)

# CORS - Allow all origins for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Middleware to attach user ID
@app.middleware("http")
async def attach_user_middleware(request: Request, call_next):
    try:
        uid = supabase_service.get_user_id_from_request(request)
        if uid:
            request.state.user_id = uid
    except Exception:
        pass
    response = await call_next(request)
    return response


def get_user_id(request: Request) -> Optional[str]:
    """Dependency to get user ID from request"""
    return getattr(request.state, "user_id", None)


# ============== Basic Routes ==============

@app.get("/")
def hello_world():
    return {"message": "Hello World - FlowBoard API"}

@app.get("/test")
async def test_route():
    """Test Vertex AI connection"""
    try:
        result = await vertex_service.test_service()
        return {"status": "success", "response": str(result)}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.get("/health")
def health_check():
    return {"status": "healthy"}


# ============== Jobs Routes ==============

@app.post("/api/jobs/video")
async def add_video_job(
    request: Request,
    files: UploadFile = File(...),
    ending_image: Optional[UploadFile] = File(None),
    global_context: str = Form(""),
    custom_prompt: str = Form(""),
    user_id: Optional[str] = Depends(get_user_id)
):
    """Start a video generation job"""
    # For MVP testing, allow unauthenticated requests
    # if not user_id:
    #     raise HTTPException(status_code=401, detail="Unauthorized")
    
    starting_image_data = await files.read()
    ending_image_data = await ending_image.read() if ending_image else None
    
    from models.job import VideoJobRequest
    data = VideoJobRequest(
        starting_image=starting_image_data,
        ending_image=ending_image_data,
        global_context=global_context,
        custom_prompt=custom_prompt
    )
    
    # Skip credit check for MVP testing without auth
    if user_id:
        # Deduct credits
        success, error = supabase_service.do_transaction(
            user_id=user_id,
            transaction_type="video_gen",
            credit_usage=10
        )
        
        if not success:
            if error == "insufficient_credits":
                raise HTTPException(status_code=402, detail="Not enough credits")
            raise HTTPException(status_code=500, detail="Transaction failed")
    
    job_id = await job_service.create_video_job(data)
    return {"job_id": job_id}


@app.get("/api/jobs/video/{job_id}")
async def get_video_job_status(job_id: str):
    """Get status of a video generation job"""
    job_status = await job_service.get_video_job_status(job_id)
    
    if not job_status:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job_status.status == "error":
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error_message": job_status.error}
        )
    
    if job_status.status == "waiting":
        return JSONResponse(
            status_code=202,
            content={
                "status": "waiting",
                "job_start_time": job_status.job_start_time.isoformat()
            }
        )
    
    return {
        "status": job_status.status,
        "job_start_time": job_status.job_start_time.isoformat(),
        "job_end_time": job_status.job_end_time.isoformat() if job_status.job_end_time else None,
        "video_url": job_status.video_url,
        "metadata": job_status.metadata
    }


# Mock endpoints for testing
@app.post("/api/jobs/video/mock")
async def add_video_job_mock(
    request: Request,
    starting_image: UploadFile = File(...),
    global_context: str = Form(""),
    custom_prompt: str = Form(""),
    user_id: Optional[str] = Depends(get_user_id)
):
    """Mock video job for testing"""
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"job_id": "mock-job-id"}


@app.get("/api/jobs/video/mock/{job_id}")
async def get_video_job_status_mock(job_id: str):
    """Mock job status for testing"""
    return {
        "status": "done",
        "job_start_time": "2024-01-01T00:00:00",
        "job_end_time": "2024-01-01T00:00:30",
        "video_url": "https://storage.googleapis.com/hackwestern_bucket/videos/sample.mp4"
    }


@app.post("/api/jobs/video/merge")
async def merge_videos(
    request: Request,
    user_id: Optional[str] = Depends(get_user_id)
):
    """Merge multiple videos into one"""
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        body = await request.json()
        video_urls = body.get("video_urls", [])
        
        if not video_urls or not isinstance(video_urls, list):
            raise HTTPException(status_code=400, detail="video_urls array is required")
        
        if len(video_urls) < 2:
            raise HTTPException(status_code=400, detail="At least 2 video URLs required")
        
        merged_video_url = await video_merge_service.merge_videos(video_urls, user_id)
        return {"video_url": merged_video_url}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ============== Supabase/User Routes ==============

@app.get("/api/supabase/me")
async def get_current_user(user_id: Optional[str] = Depends(get_user_id)):
    """Get current user's profile"""
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    user_row = supabase_service.get_user_row(user_id)
    if not user_row or not user_row.data:
        raise HTTPException(status_code=404, detail="User not found")
    
    return user_row.data


@app.get("/api/supabase/transactions")
async def get_transactions(user_id: Optional[str] = Depends(get_user_id)):
    """Get user's transaction history"""
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    transactions = supabase_service.get_transaction_log(user_id)
    return transactions.data if transactions else []


# ============== Gemini Routes ==============

@app.post("/api/gemini/image")
async def generate_image(
    request: Request,
    image: UploadFile = File(...),
    user_id: Optional[str] = Depends(get_user_id)
):
    """Improve/generate image using Gemini"""
    # For MVP testing, allow unauthenticated requests
    # if not user_id:
    #     raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        image_data = await image.read()
        
        # Skip credit check for MVP testing without auth
        if user_id:
            # Check and deduct credits
            success, error = supabase_service.do_transaction(
                user_id=user_id,
                transaction_type="image_gen",
                credit_usage=1
            )
            
            if not success:
                if error == "insufficient_credits":
                    raise HTTPException(status_code=402, detail="Not enough credits")
                raise HTTPException(status_code=500, detail="Transaction failed")
        
        prompt = "Improve the attached image and fill in any missing details. Do not deviate from the original art style too much, simply understand the artist's idea and enhance it a bit."
        
        result = await vertex_service.generate_image_content(
            prompt=prompt,
            image=image_data
        )
        
        return {"image_bytes": result}
        
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/gemini/extract-context")
async def extract_context(
    request: Request,
    video: UploadFile = File(...),
    user_id: Optional[str] = Depends(get_user_id)
):
    """Extract context from video using Gemini"""
    if not user_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        video_data = await video.read()
        
        prompt = (
            "Extract structured scene information from this video.\n"
            "Respond with ONLY valid JSON. No explanations, no markdown, no backticks.\n"
            "Follow this exact structure, keys required:\n"
            "{\n"
            '  "entities": [\n'
            '    { "id": "id-1", "description": "...", "appearance": "..." }\n'
            "  ],\n"
            '  "environment": "...",\n'
            '  "style": "..."\n'
            "}\n"
            "If information is missing, use empty strings.\n"
        )
        
        import json as pyjson
        
        # Create a simple object to hold video data
        class VideoData:
            def __init__(self, data):
                self.data = data
        
        res = vertex_service.analyze_video_content(
            prompt=prompt,
            video_data=VideoData(video_data)
        )
        
        raw = res.text or res.candidates[0].content.parts[0].text
        
        # Strip markdown if present
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split('\n')
            cleaned = '\n'.join(lines[1:])
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        
        try:
            parsed = pyjson.loads(cleaned)
            return parsed
        except Exception:
            raise HTTPException(status_code=500, detail=f"Failed to parse JSON: {raw}")
        
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
