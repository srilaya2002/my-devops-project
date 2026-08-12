"""Deployment routes"""
from fastapi import APIRouter, HTTPException, status, BackgroundTasks
import logging
from app.config import settings
from app.deployer import AnsibleMssqlDeployer

router = APIRouter()
logger = logging.getLogger(__name__)

deployer = AnsibleMssqlDeployer()


@router.get("/status")
async def get_deployment_status():
    """Get current deployment status"""
    history = deployer.get_history()
    latest = history[0] if history else None
    return {
        "status": latest["status"] if latest else "ready",
        "latest_task": latest,
        "engine": "ansible",
        "mssql_version": settings.MSSQL_VERSION,
        "mssql_edition": settings.MSSQL_EDITION,
        "vm1": settings.VM1_HOST,
        "vm2": settings.VM2_HOST
    }


@router.post("/install")
async def deploy_install(background_tasks: BackgroundTasks):
    """Deploy and install MSSQL on all servers
    
    This is a long-running operation (30-60 minutes).
    Returns immediately with a status ID.
    """
    
    logger.info("Received deployment request - Install MSSQL")
    
    try:
        task_id = deployer.start_task("install")
        background_tasks.add_task(deployer.deploy_install, task_id)
        
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "MSSQL installation started",
            "engine": "ansible",
            "playbook": "site.yml",
            "estimated_duration_minutes": 45,
            "instructions": "Check /api/v1/deploy/status or /api/v1/deploy/history for progress"
        }
    
    except Exception as e:
        logger.error(f"Error initiating deployment: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate deployment: {str(e)}"
        )


@router.post("/backup")
async def deploy_backup(background_tasks: BackgroundTasks):
    """Create 10-stripe backup on VM1 and transfer to VM2
    
    Prerequisites: MSSQL must be installed and AdventureWorks DB must exist
    This operation takes 10-20 minutes depending on database size.
    """
    
    logger.info("Received deployment request - Backup and Restore")
    
    try:
        task_id = deployer.start_task("backup-restore")
        background_tasks.add_task(deployer.deploy_backup_restore, task_id)
        
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Backup and restore process started",
            "engine": "ansible",
            "playbook": "backup.yml",
            "operations": [
                "Create 10-stripe backup on VM1",
                "Transfer backup files to VM2",
                "Restore AdventureWorks on VM2"
            ],
            "estimated_duration_minutes": 15
        }
    
    except Exception as e:
        logger.error(f"Error initiating backup: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate backup: {str(e)}"
        )


@router.post("/install-tools")
async def deploy_install_tools(background_tasks: BackgroundTasks):
    """Install MSSQL tools only (sqlcmd) without database"""
    
    logger.info("Received deployment request - Install tools only")
    
    try:
        task_id = deployer.start_task("install-tools")
        background_tasks.add_task(deployer.install_tools, task_id)
        
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "MSSQL tools installation started",
            "engine": "ansible",
            "playbook": "site.yml",
            "tags": ["install", "tools"],
            "components": ["mssql-tools", "sqlcmd"],
            "estimated_duration_minutes": 10
        }
    
    except Exception as e:
        logger.error(f"Error initiating tools installation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate tools installation: {str(e)}"
        )


@router.post("/build")
async def deploy_build(background_tasks: BackgroundTasks):
    """Prepare hosts and perform MSSQL build tasks (idempotent).

    This endpoint runs the "build.yml" playbook which includes the
    `mssql_build` role: a full idempotent SQL Server install (dirs, service
    account, repos, packages, mssql-conf setup, and verification).
    """

    logger.info("Received deployment request - MSSQL build")

    try:
        task_id = deployer.start_task("build")
        background_tasks.add_task(deployer.deploy_build, task_id)

        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "MSSQL build started (prepare + install)",
            "engine": "ansible",
            "playbook": "build.yml",
            "estimated_duration_minutes": 10,
            "notes": "Runs the mssql_build role's full install flow; safe to re-run (idempotent)."
        }

    except Exception as e:
        logger.error(f"Error initiating MSSQL build: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate MSSQL build: {str(e)}"
        )


@router.post("/restore-db")
async def deploy_restore_db(background_tasks: BackgroundTasks):
    """Restore AdventureWorks database to VM1 only."""
    
    logger.info("Received deployment request - Restore database")
    
    try:
        task_id = deployer.start_task("restore-adventureworks")
        background_tasks.add_task(deployer.restore_adventureworks, task_id)
        
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Database restore started on VM1",
            "engine": "ansible",
            "playbook": "site.yml",
            "tags": ["adventureworks"],
            "database": "AdventureWorks",
            "operations": [
                "Download AdventureWorks2019.bak on VM1",
                "Restore AdventureWorks on VM1"
            ],
            "estimated_duration_minutes": 10
        }
    
    except Exception as e:
        logger.error(f"Error initiating database restore: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate database restore: {str(e)}"
        )


@router.post("/alwayson")
async def deploy_alwayson(background_tasks: BackgroundTasks):
    """Configure Always-On Availability Group across VM1 and VM2"""
    logger.info("Received deployment request - Configure Always On")
    try:
        task_id = deployer.start_task("alwayson")
        background_tasks.add_task(deployer.deploy_alwayson, task_id)

        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Always On availability group deployment started",
            "engine": "ansible",
            "playbook": "alwayson.yml",
            "estimated_duration_minutes": 30,
            "operations": [
                "Verify MSSQL connectivity",
                "Create AG endpoints",
                "Create and join availability group",
                "Verify AG health"
            ]
        }
    except Exception as e:
        logger.error(f"Error initiating Always-On deployment: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate Always-On deployment: {str(e)}"
        )


@router.post("/full-ag")
async def deploy_full_ag(background_tasks: BackgroundTasks):
    """Restore AdventureWorks to VM1, create a striped backup, copy to VM2, restore there, and configure AG."""
    logger.info("Received deployment request - Full AG workflow")
    try:
        task_id = deployer.start_task("full-ag")
        background_tasks.add_task(deployer.deploy_full_ag, task_id)

        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Full AdventureWorks backup/restore and Always On workflow started",
            "engine": "ansible",
            "playbooks": ["site.yml", "backup.yml", "alwayson.yml"],
            "operations": [
                "Restore AdventureWorks on VM1",
                "Create 10-stripe backup on VM1",
                "Transfer backup to VM2 and restore",
                "Configure Always On availability group"
            ],
            "estimated_duration_minutes": 90,
        }
    except Exception as e:
        logger.error(f"Error initiating full AG workflow: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to initiate full AG workflow: {str(e)}"
        )


@router.get("/history")
async def get_deployment_history():
    """Get deployment execution history"""
    try:
        history = deployer.get_history()
        return {
            "total_executions": len(history),
            "executions": history
        }
    except Exception as e:
        logger.error(f"Error retrieving history: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve history: {str(e)}"
        )


@router.get("/hosts")
async def get_target_hosts():
    """Get target host information"""
    try:
        inventory = deployer.get_hosts()
        return {
            "status": "success",
            "inventory": inventory,
            "dns": deployer.resolve_hosts(),
            "ansible_inventory": settings.ANSIBLE_INVENTORY,
            "ansible_playbooks": settings.ANSIBLE_PLAYBOOK_DIR,
        }
    except Exception as e:
        logger.error(f"Error retrieving hosts: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve hosts: {str(e)}"
        )


@router.post("/ping")
async def ping_hosts():
    """Ping all target hosts to verify connectivity"""
    try:
        result = deployer.ping_hosts()
        return result
    except Exception as e:
        logger.error(f"Error pinging hosts: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to ping hosts: {str(e)}"
        )

@router.post("/teardown")
async def deploy_teardown(background_tasks: BackgroundTasks):
    """Tear down the Always On AG, AdventureWorks, and backup artifacts.

    Leaves MSSQL installed and configured. Safe to re-run.
    """
    logger.info("Received deployment request - Teardown")
    try:
        task_id = deployer.start_task("teardown")
        background_tasks.add_task(deployer.deploy_teardown, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Teardown started",
            "engine": "ansible",
            "playbook": "teardown.yml",
            "estimated_duration_minutes": 5,
        }
    except Exception as e:
        logger.error(f"Error initiating teardown: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate teardown: {str(e)}")


@router.post("/rewind")
async def deploy_rewind(background_tasks: BackgroundTasks):
    """Tear down AG/AdventureWorks/backups, then restore a fresh AdventureWorks on vm1.

    Leaves the lab ready to retry backup/restore/alwayson from a clean baseline.
    """
    logger.info("Received deployment request - Rewind")
    try:
        task_id = deployer.start_task("rewind")
        background_tasks.add_task(deployer.deploy_rewind, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Rewind started",
            "engine": "ansible",
            "playbooks": ["teardown.yml", "site.yml"],
            "estimated_duration_minutes": 15,
        }
    except Exception as e:
        logger.error(f"Error initiating rewind: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate rewind: {str(e)}")


@router.post("/reset-baseline")
async def deploy_reset_baseline(background_tasks: BackgroundTasks):
    """Uninstall MSSQL entirely and return both hosts to a bare VM.

    Does not reinstall -- call POST /api/v1/deploy/install afterward to rebuild.
    """
    logger.info("Received deployment request - Reset baseline")
    try:
        task_id = deployer.start_task("reset-baseline")
        background_tasks.add_task(deployer.deploy_reset_baseline, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "Reset to bare-VM baseline started",
            "engine": "ansible",
            "playbook": "uninstall.yml",
            "estimated_duration_minutes": 5,
        }
    except Exception as e:
        logger.error(f"Error initiating reset-baseline: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate reset-baseline: {str(e)}")


@router.get("/rewind-plan")
async def get_rewind_plan():
    """Return the step-by-step plan for teardown/rewind/reset-baseline without executing anything."""
    try:
        return deployer.get_rewind_plan()
    except Exception as e:
        logger.error(f"Error retrieving rewind plan: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to retrieve rewind plan: {str(e)}")

@router.post("/ag-status")
async def deploy_ag_status(background_tasks: BackgroundTasks):
    """Snapshot the Always On AG's replica roles and sync state. Read-only, safe any time."""
    logger.info("Received deployment request - AG status")
    try:
        task_id = deployer.start_task("ag-status")
        background_tasks.add_task(deployer.deploy_ag_status, task_id)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": "AG status check started",
            "engine": "ansible",
            "playbook": "ag_status.yml",
            "estimated_duration_minutes": 1,
        }
    except Exception as e:
        logger.error(f"Error checking AG status: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to check AG status: {str(e)}")


@router.post("/failover")
async def deploy_failover(background_tasks: BackgroundTasks, target: str, mode: str = "planned"):
    """Fail the Always On AG over to `target` ('vm1' or 'vm2').

    mode=planned (default) requires target to already be SYNCHRONIZED -- no data loss.
    mode=forced uses FORCE_FAILOVER_ALLOW_DATA_LOSS -- only when the current primary is unreachable.
    """
    if target not in ("vm1", "vm2"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm1' or 'vm2'")
    if mode not in ("planned", "forced"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mode must be 'planned' or 'forced'")

    logger.info(f"Received deployment request - Failover to {target} ({mode})")
    try:
        task_id = deployer.start_task(f"failover-{target}-{mode}")
        background_tasks.add_task(deployer.deploy_failover, task_id, target, mode)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": f"Failover to {target} ({mode}) started",
            "engine": "ansible",
            "playbook": "failover.yml",
            "target": target,
            "mode": mode,
            "estimated_duration_minutes": 3,
        }
    except Exception as e:
        logger.error(f"Error initiating failover: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate failover: {str(e)}")        

@router.post("/sync-rebuild")
async def deploy_sync_rebuild(background_tasks: BackgroundTasks, target: str):
    """Resynchronize or rejoin `target` ('vm1' or 'vm2') to the Always On AG.

    Resumes suspended data movement if the replica is still a member, or
    rejoins (and lets automatic seeding reseed it) if it fell out of the AG.
    """
    if target not in ("vm1", "vm2"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="target must be 'vm1' or 'vm2'")

    logger.info(f"Received deployment request - Sync rebuild {target}")
    try:
        task_id = deployer.start_task(f"sync-rebuild-{target}")
        background_tasks.add_task(deployer.deploy_sync_rebuild, task_id, target)
        return {
            "status": "initiated",
            "task_id": task_id,
            "message": f"Sync rebuild for {target} started",
            "engine": "ansible",
            "playbook": "sync_rebuild.yml",
            "target": target,
            "estimated_duration_minutes": 15,
        }
    except Exception as e:
        logger.error(f"Error initiating sync rebuild: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to initiate sync rebuild: {str(e)}")