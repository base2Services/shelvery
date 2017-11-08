import boto3

from shelvery.runtime_config import RuntimeConfig
from shelvery.backup_resource import BackupResource
from shelvery.engine import ShelveryEngine
from shelvery.entity_resource import EntityResource

from typing import Dict, List


class ShelveryRDSBackup(ShelveryEngine):
    def is_backup_available(self, backup_region: str, backup_id: str) -> bool:
        rds_client = boto3.client('rds', region_name=backup_region)
        snapshots = rds_client.describe_db_snapshots(DBSnapshotIdentifier=backup_id)
        return snapshots['DBSnapshots'][0]['Status'] == 'available'
    
    def get_resource_type(self) -> str:
        return 'RDS Instance'
    
    def backup_resource(self, backup_resource: BackupResource) -> BackupResource:
        if RuntimeConfig.get_rds_mode() == RuntimeConfig.RDS_CREATE_SNAPSHOT:
            return self.backup_from_instance(backup_resource)
        if RuntimeConfig.get_rds_mode() == RuntimeConfig.RDS_COPY_AUTOMATED_SNAPSHOT:
            return self.backup_from_latest_automated(backup_resource)
        
        raise Exception(f"Only {RuntimeConfig.RDS_COPY_AUTOMATED_SNAPSHOT} and "
                        f"{RuntimeConfig.RDS_CREATE_SNAPSHOT} rds backup "
                        f"modes supported - set rds backup mode using rds_backup_mode configuration option ")
    
    def backup_from_latest_automated(self, backup_resource: BackupResource):
        rds_client = boto3.client('rds')
        auto_snapshots = rds_client.describe_db_snapshots(
            DBInstanceIdentifier=backup_resource.entity_id,
            SnapshotType='automated',
            # API always returns in date descending order, and we only need last one
            MaxRecords=20
        )
        auto_snapshots = sorted(auto_snapshots['DBSnapshots'], key=lambda k: k['SnapshotCreateTime'], reverse=True)
        
        # TODO handle case when there are no latest automated backups
        automated_snapshot_id = auto_snapshots[0]['DBSnapshotIdentifier']
        rds_client.copy_db_snapshot(
            SourceDBSnapshotIdentifier=automated_snapshot_id,
            TargetDBSnapshotIdentifier=backup_resource.name,
            CopyTags=False
        )
        backup_resource.backup_id = backup_resource.name
        return backup_resource
    
    def backup_from_instance(self, backup_resource):
        rds_client = boto3.client('rds')
        rds_client.create_db_snapshot(
            DBSnapshotIdentifier=backup_resource.name,
            DBInstanceIdentifier=backup_resource.entity_id
        )
        backup_resource.backup_id = backup_resource.name
        return backup_resource
    
    def delete_backup(self, backup_resource: BackupResource):
        rds_client = boto3.client('rds')
        rds_client.delete_db_snapshot(
            DBInstanceIdentifier=backup_resource.entity_id
        )
    
    def tag_backup_resource(self, backup_resource: BackupResource):
        rds_client = boto3.client('rds')
        snapshots = rds_client.describe_db_snapshots(DBSnapshotIdentifier=backup_resource.backup_id)
        snapshot_arn = snapshots['DBSnapshots'][0]['DBSnapshotArn']
        rds_client.add_tags_to_resource(
            ResourceName=snapshot_arn,
            Tags=list(map(lambda k: {'Key': k, 'Value': backup_resource.tags[k]}, backup_resource.tags))
        )
    
    def get_existing_backups(self, backup_tag_prefix: str) -> List[BackupResource]:
        rds_client = boto3.client('rds')
        
        # collect all snapshots
        all_snapshots = self.collect_all_snapshots(rds_client)
        
        # filter ones backed up with shelvery
        all_backups = self.get_shelvery_backups_only(all_snapshots, backup_tag_prefix, rds_client)
        
        return all_backups
    
    def share_backup_with_account(self, backup_region: str, backup_id: str, aws_account_id: str):
        rds_client = boto3.client('rds', region_name=backup_region)
        rds_client.modify_db_snapshot_attribute(
            DBSnapshotIdentifier=backup_id,
            AttributeName='restore',
            ValuesToAdd=[aws_account_id]
        )
    
    def copy_backup_to_region(self, backup_id: str, region: str) -> str:
        local_region = boto3.session.Session().region_name
        client_local = boto3.client('rds')
        rds_client = boto3.client('rds', region_name=region)
        snapshots = client_local.describe_db_snapshots(DBSnapshotIdentifier=backup_id)
        snapshot = snapshots['DBSnapshots'][0]
        rds_client.copy_db_snapshot(
            SourceDBSnapshotIdentifier=snapshot['DBSnapshotArn'],
            TargetDBSnapshotIdentifier=backup_id,
            SourceRegion=local_region,
            # tags are created explicitly
            CopyTags=False
        )
        return backup_id
    
    def get_backup_resource(self, backup_region: str, backup_id: str) -> BackupResource:
        rds_client = boto3.client('rds', region_name=backup_region)
        snapshots = rds_client.describe_db_snapshots(DBSnapshotIdentifier=backup_id)
        snapshot = snapshots['DBSnapshots'][0]
        tags = rds_client.list_tags_for_resource(ResourceName=snapshot['DBSnapshotArn'])['TagList']
        d_tags = dict(map(lambda t: (t['Key'], t['Value']), tags))
        return BackupResource.construct(d_tags['shelvery:tag_name'], backup_id, d_tags)
    
    def get_engine_type(self) -> str:
        return 'rds'
    
    def get_entities_to_backup(self, tag_name: str) -> List[EntityResource]:
        # region and api client
        local_region = boto3.session.Session().region_name
        rds_client = boto3.client('rds')
        
        # list of models returned from api
        db_entities = []
        
        db_instances = self.get_all_instances(rds_client)
        
        # collect tags in check if instance tagged with marker tag
        
        for instance in db_instances:
            tags = rds_client.list_tags_for_resource(ResourceName=instance['DBInstanceArn'])['TagList']
            
            # convert api response to dictionary
            d_tags = dict(map(lambda t: (t['Key'], t['Value']), tags))
            
            # check if marker tag is present
            if tag_name in d_tags:
                resource = EntityResource(instance['DBInstanceIdentifier'],
                                          local_region,
                                          instance['InstanceCreateTime'],
                                          d_tags)
                db_entities.append(resource)
        
        return db_entities
    
    def get_all_instances(self, rds_client):
        """
        Get all RDS instances within region for given boto3 client
        :param rds_client: boto3 rds service
        :return: all RDS instances within region for given boto3 client
        """
        # list of resource models
        db_instances = []
        # temporary list of api models, as calls are batched
        temp_instances = rds_client.describe_db_instances()
        db_instances.extend(temp_instances['DBInstances'])
        # collect database instances
        while 'Marker' in temp_instances:
            temp_instances = rds_client.describe_db_instances(Marker=temp_instances['Marker'])
            db_instances.extend(temp_instances['DBInstances'])
        
        return db_instances
    
    def get_shelvery_backups_only(self, all_snapshots, backup_tag_prefix, rds_client):
        """
        :param all_snapshots: all snapshots within region
        :param backup_tag_prefix:  prefix of shelvery backup system
        :param rds_client:  amazon boto3 rds client
        :return: snapshots created using shelvery
        """
        all_backups = []
        marker_tag = f"{backup_tag_prefix}:{BackupResource.BACKUP_MARKER_TAG}"
        for snap in all_snapshots:
            tags = rds_client.list_tags_for_resource(ResourceName=snap['DBSnapshotArn'])['TagList']
            self.logger.info(f"Checking RDS Snap {snap['DBSnapshotIdentifier']}")
            d_tags = dict(map(lambda t: (t['Key'], t['Value']), tags))
            if marker_tag in d_tags:
                all_backups.append(BackupResource.construct(backup_tag_prefix, snap['DBSnapshotIdentifier'], d_tags))
        return all_backups
    
    def collect_all_snapshots(self, rds_client):
        """
        :param rds_client:
        :return: All snapshots within region for rds_client
        """
        all_snapshots = []
        tmp_snapshots = rds_client.describe_db_snapshots()
        all_snapshots.extend(tmp_snapshots['DBSnapshots'])
        while 'Marker' in tmp_snapshots:
            tmp_snapshots = rds_client.describe_db_snapshots()
            all_snapshots.extend(tmp_snapshots['DBSnapshots'])
        
        return all_snapshots
